const CLICK_ID_KEYS = [
  'gclid', 'gbraid', 'wbraid', 'fbclid', 'ttclid',
  'li_fat_id', 'msclkid', 'twclid', 'epik',
];

const PLATFORM_COOKIE_KEYS = [
  '_fbp', '_fbc', '_ga', '_gcl_aw', '_gcl_gb', '_ttp', 'ttp',
];
const PUBLIC_SECOND_LEVEL_SUFFIXES = new Set(['ac', 'co', 'com', 'edu', 'gov', 'net', 'org']);

const SDK_PATH = '/sdk/versya-messaging.js';
const SDK_VERSION_PATH = '/sdk/version';
const API_ALIAS_PATHS = new Set(['/track', '/page', '/identify']);
const API_PROXY_PREFIXES = [
  '/api/v1/track',
  '/api/v1/page',
  '/api/v1/identify',
  '/api/v1/verify-install',
  '/api/v1/consent',
  '/api/v1/alias',
  '/api/v1/visitor-data',
  '/api/v1/recover-identity',
  '/api/v1/config',
];

function corsHeaders(request) {
  const origin = request.headers.get('Origin') || '*';
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-Write-Key, X-SDK-Version, Authorization',
    'Vary': 'Origin',
  };
}

function parseCookies(header) {
  const cookies = {};
  if (!header) return cookies;
  header.split(';').forEach((part) => {
    const idx = part.indexOf('=');
    if (idx <= 0) return;
    const key = part.slice(0, idx).trim();
    const value = part.slice(idx + 1).trim();
    if (key) cookies[key] = decodeURIComponent(value);
  });
  return cookies;
}

function parentCookieDomain(hostname) {
  const parts = hostname.split('.');
  if (parts.length < 3) return null;
  if (parts.length >= 4 && parts[parts.length - 1].length === 2 && PUBLIC_SECOND_LEVEL_SUFFIXES.has(parts[parts.length - 2])) {
    return `.${parts.slice(-3).join('.')}`;
  }
  return `.${parts.slice(-2).join('.')}`;
}

function cookieString(name, value, maxAge, domain) {
  const pieces = [
    `${name}=${encodeURIComponent(value)}`,
    'Path=/',
    `Max-Age=${maxAge}`,
    'SameSite=Lax',
    'Secure',
  ];
  if (domain) pieces.push(`Domain=${domain}`);
  return pieces.join('; ');
}

function deriveFbc(fbclid) {
  return `fb.1.${Date.now()}.${fbclid}`;
}

function collectTrackingState(url, cookies) {
  const clickIds = {};
  CLICK_ID_KEYS.forEach((key) => {
    const fromQuery = url.searchParams.get(key);
    const fromCookie = cookies[`_vsya_${key}`];
    const value = fromQuery || fromCookie;
    if (value) clickIds[key] = value;
  });

  const platformCookies = {};
  PLATFORM_COOKIE_KEYS.forEach((key) => {
    if (cookies[key]) platformCookies[key] = cookies[key];
  });
  if (!platformCookies._fbc && clickIds.fbclid) {
    platformCookies._fbc = deriveFbc(clickIds.fbclid);
  }

  return { clickIds, platformCookies };
}

function buildSetCookies(hostname, config, clickIds, platformCookies) {
  if (!config.cookie_keeper_enabled) return [];
  const domain = config.cookie_domain || parentCookieDomain(hostname);
  const setCookies = [];
  const clickMaxAge = 180 * 24 * 60 * 60;
  const browserMaxAge = 2 * 365 * 24 * 60 * 60;

  Object.entries(clickIds).forEach(([key, value]) => {
    setCookies.push(cookieString(`_vsya_${key}`, value, clickMaxAge, domain));
  });
  Object.entries(platformCookies).forEach(([key, value]) => {
    const maxAge = key === '_ga' || key === '_fbp' || key === '_ttp' || key === 'ttp'
      ? browserMaxAge
      : clickMaxAge;
    setCookies.push(cookieString(key, value, maxAge, domain));
  });
  return setCookies;
}

async function getTenantConfig(hostname, env) {
  const cacheKey = `tracking:${hostname}`;
  if (env.TENANT_CONFIG_KV) {
    const cached = await env.TENANT_CONFIG_KV.get(cacheKey, { type: 'json' });
    if (cached) return cached;
  }

  const apiOrigin = (env.VERSYA_API_ORIGIN || '').replace(/\/$/, '');
  if (!apiOrigin || !env.EDGE_CONFIG_SECRET) {
    throw new Error('Tracking proxy is missing VERSYA_API_ORIGIN or EDGE_CONFIG_SECRET');
  }

  const url = `${apiOrigin}/api/v1/internal/tracking-domains/edge-config?hostname=${encodeURIComponent(hostname)}`;
  const response = await fetch(url, {
    headers: { 'X-Versya-Edge-Secret': env.EDGE_CONFIG_SECRET },
  });
  if (!response.ok) {
    throw new Error(`Tenant config lookup failed: ${response.status}`);
  }
  const config = await response.json();
  if (env.TENANT_CONFIG_KV) {
    await env.TENANT_CONFIG_KV.put(cacheKey, JSON.stringify(config), { expirationTtl: 300 });
  }
  return config;
}

function mapApiPath(pathname) {
  if (API_ALIAS_PATHS.has(pathname)) return `/api/v1${pathname}`;
  return pathname;
}

function shouldProxyApi(pathname) {
  return API_ALIAS_PATHS.has(pathname) || API_PROXY_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}?`));
}

async function enrichJsonBody(request, trackingState) {
  const contentType = request.headers.get('Content-Type') || '';
  if (!contentType.includes('application/json') || request.method === 'GET') {
    return request.body;
  }
  const text = await request.text();
  if (!text) return text;
  try {
    const body = JSON.parse(text);
    body.context = body.context || {};
    body.context.attribution = body.context.attribution || {};
    body.context.attribution.click_ids = {
      ...(body.context.attribution.click_ids || {}),
      ...trackingState.clickIds,
    };
    body.context.attribution.cookies = {
      ...(body.context.attribution.cookies || {}),
      ...trackingState.platformCookies,
    };
    if (body.event_id) {
      body.context.event_id = body.context.event_id || body.event_id;
      body.properties = body.properties || {};
      body.properties.event_id = body.properties.event_id || body.event_id;
    }
    return JSON.stringify(body);
  } catch (_) {
    return text;
  }
}

async function logProxyEvent(ctx, env, config, request, response, trackingState, bodyText) {
  if (!env.EDGE_CONFIG_SECRET || !config.api_origin) return;
  let eventId = null;
  let anonymousId = null;
  try {
    if (bodyText && typeof bodyText === 'string' && bodyText.startsWith('{')) {
      const parsed = JSON.parse(bodyText);
      eventId = parsed.event_id || parsed.properties?.event_id || null;
      anonymousId = parsed.anonymous_id || null;
    }
  } catch (_) {}

  const apiOrigin = config.api_origin.replace(/\/$/, '');
  const url = `${apiOrigin}/api/v1/internal/tracking-domains/proxy-log`;
  const payload = {
    hostname: config.hostname,
    method: request.method,
    path: new URL(request.url).pathname,
    action: new URL(request.url).pathname.includes('/sdk/') ? 'sdk' : 'ingest',
    event_id: eventId,
    anonymous_id: anonymousId,
    status_code: response.status,
    click_ids: trackingState.clickIds,
    cookies_refreshed: Object.keys({ ...trackingState.clickIds, ...trackingState.platformCookies }),
    request_summary: {
      proxy_version: env.PROXY_VERSION || 'dev',
      has_body: Boolean(bodyText),
    },
  };
  ctx.waitUntil(fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Versya-Edge-Secret': env.EDGE_CONFIG_SECRET,
    },
    body: JSON.stringify(payload),
  }).catch(() => {}));
}

function withSetCookies(response, request, setCookies) {
  const headers = new Headers(response.headers);
  Object.entries(corsHeaders(request)).forEach(([key, value]) => headers.set(key, value));
  headers.set('X-Versya-Tracking-Proxy', 'true');
  setCookies.forEach((cookie) => headers.append('Set-Cookie', cookie));
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function proxySdk(request, env, ctx, config, trackingState, setCookies) {
  const source = (config.sdk_origin || config.api_origin).replace(/\/$/, '');
  const url = new URL(request.url);
  const target = `${source}${url.pathname}${url.search}`;
  const headers = new Headers(request.headers);
  headers.set('X-Versya-Tracking-Host', config.hostname);
  headers.set('X-Versya-Proxy-Version', env.PROXY_VERSION || 'dev');
  headers.set('Cache-Control', 'no-cache');
  headers.delete('Host');
  const response = await fetch(target, { headers, cf: { cacheTtl: 0 } });
  const finalResponse = withSetCookies(response, request, setCookies);
  finalResponse.headers.set('Cache-Control', 'public, max-age=0, must-revalidate');
  finalResponse.headers.set('CDN-Cache-Control', 'public, max-age=60, must-revalidate');
  await logProxyEvent(ctx, env, config, request, finalResponse, trackingState, null);
  return finalResponse;
}

async function proxyApi(request, env, ctx, config, trackingState, setCookies) {
  const requestUrl = new URL(request.url);
  const mappedPath = mapApiPath(requestUrl.pathname);
  const target = `${config.api_origin.replace(/\/$/, '')}${mappedPath}${requestUrl.search}`;
  const headers = new Headers(request.headers);
  headers.set('X-Write-Key', config.write_key);
  headers.set('X-Versya-Tracking-Host', config.hostname);
  headers.set('X-Versya-Proxy-Version', env.PROXY_VERSION || 'dev');
  headers.set('X-Original-Host', request.headers.get('Host') || config.hostname);
  headers.delete('Host');

  const body = await enrichJsonBody(request.clone(), trackingState);
  const response = await fetch(target, {
    method: request.method,
    headers,
    body: request.method === 'GET' || request.method === 'HEAD' ? undefined : body,
    redirect: 'manual',
  });
  const finalResponse = withSetCookies(response, request, setCookies);
  await logProxyEvent(ctx, env, config, request, finalResponse, trackingState, body);
  return finalResponse;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }

    const url = new URL(request.url);
    const hostname = url.hostname.toLowerCase();
    let config;
    try {
      config = await getTenantConfig(hostname, env);
    } catch (error) {
      return new Response(JSON.stringify({ error: error.message }), {
        status: 404,
        headers: { 'Content-Type': 'application/json', ...corsHeaders(request) },
      });
    }

    const cookies = parseCookies(request.headers.get('Cookie'));
    const trackingState = collectTrackingState(url, cookies);
    const setCookies = buildSetCookies(hostname, config, trackingState.clickIds, trackingState.platformCookies);

    if (url.pathname === SDK_PATH || url.pathname === SDK_VERSION_PATH) {
      return proxySdk(request, env, ctx, config, trackingState, setCookies);
    }
    if (shouldProxyApi(url.pathname)) {
      return proxyApi(request, env, ctx, config, trackingState, setCookies);
    }

    return new Response(JSON.stringify({ error: 'Unsupported tracking proxy path' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json', ...corsHeaders(request) },
    });
  },
};
