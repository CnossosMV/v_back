/**
 * Versya Messaging SDK
 * Event tracking and user identification for Versya Messaging Middleware
 *
 * Usage:
 * <script>
 * (function(w,d,s,k,u){
 *   w.versya=w.versya||{_q:[],
 *     init:function(){this._q.push(['init',arguments])},
 *     identify:function(){this._q.push(['identify',arguments])},
 *     track:function(){this._q.push(['track',arguments])},
 *     page:function(){this._q.push(['page',arguments])},
 *     reset:function(){this._q.push(['reset',arguments])}
 *   };
 *   var f=d.getElementsByTagName(s)[0],j=d.createElement(s);
 *   j.async=1;j.src=u+'/sdk/versya-messaging.js?v=VERSION';
 *   f.parentNode.insertBefore(j,f);
 *   w.versya.init(k);
 * })(window,document,'script','pk_live_YOUR_WRITE_KEY','https://your-api-url.com');
 * </script>
 */

(function(window, document) {
  'use strict';

  // Configuration
  var VERSION = '2.4.0';
  var STORAGE_KEY = 'versya_messaging';
  var ANONYMOUS_ID_KEY = 'versya_anonymous_id';
  var INSTALL_VERIFIED_KEY = 'versya_install_verified';
  var CONSENT_KEY = 'versya_consent';
  var CONFIG_KEY = 'versya_config';
  var CONFIG_TS_KEY = 'versya_config_ts';
  var CONFIG_TTL = 300000; // 5 minutes
  var COOKIE_NAME = '_vsya_vid';
  var COOKIE_MAX_AGE = 63072000; // 2 years
  var CLICK_COOKIE_MAX_AGE = 15552000; // 180 days
  var VERSION_CHECK_KEY = 'versya_version_checked';
  var SESSION_KEY = 'versya_session';
  var SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

  // Cookie helpers for cross-subdomain visitor identification
  function _detectRootDomain() {
    var hostname = window.location.hostname;
    if (/^(\d{1,3}\.){3}\d{1,3}$/.test(hostname) || hostname === 'localhost') {
      return hostname;
    }
    var parts = hostname.split('.');
    for (var i = parts.length - 2; i >= 0; i--) {
      var testDomain = '.' + parts.slice(i).join('.');
      document.cookie = '_vsya_test=1; domain=' + testDomain + '; path=/; max-age=1; SameSite=Lax';
      if (document.cookie.indexOf('_vsya_test=1') !== -1) {
        document.cookie = '_vsya_test=; domain=' + testDomain + '; path=/; max-age=0';
        return testDomain;
      }
    }
    return hostname;
  }

  function getCookieVisitorId() {
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf(COOKIE_NAME + '=') === 0) {
        return c.substring(COOKIE_NAME.length + 1);
      }
    }
    return null;
  }

  function setCookieVisitorId(id, domain) {
    var cookie = COOKIE_NAME + '=' + id + '; path=/; max-age=' + COOKIE_MAX_AGE + '; SameSite=Lax';
    if (domain) cookie += '; domain=' + domain;
    if (window.location.protocol === 'https:') cookie += '; Secure';
    document.cookie = cookie;
  }

  function clearCookieVisitorId(domain) {
    var cookie = COOKIE_NAME + '=; path=/; max-age=0';
    if (domain) cookie += '; domain=' + domain;
    document.cookie = cookie;
  }

  // ==========================================
  // Device Fingerprinting (Tier 5 — hint only)
  // ==========================================

  var FINGERPRINT_KEY = 'versya_device_fp';

  function _fnv1aHash(str) {
    var hash = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) {
      hash ^= str.charCodeAt(i);
      hash = (hash * 0x01000193) >>> 0;
    }
    return ('00000000' + hash.toString(16)).slice(-8);
  }

  function _getCanvasFingerprint() {
    try {
      var canvas = document.createElement('canvas');
      canvas.width = 200;
      canvas.height = 50;
      var ctx = canvas.getContext('2d');
      if (!ctx) return '';
      ctx.textBaseline = 'top';
      ctx.font = '14px Arial';
      ctx.fillStyle = '#f60';
      ctx.fillRect(0, 0, 100, 50);
      ctx.fillStyle = '#069';
      ctx.fillText('Versya.fp', 2, 15);
      ctx.fillStyle = 'rgba(102,204,0,0.7)';
      ctx.fillText('Versya.fp', 4, 17);
      ctx.globalCompositeOperation = 'multiply';
      ctx.fillStyle = 'rgb(255,0,255)';
      ctx.beginPath();
      ctx.arc(50, 25, 20, 0, Math.PI * 2, true);
      ctx.closePath();
      ctx.fill();
      return _fnv1aHash(canvas.toDataURL());
    } catch (e) {
      return '';
    }
  }

  function _getWebGLFingerprint() {
    try {
      var canvas = document.createElement('canvas');
      var gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
      if (!gl) return '';
      var debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
      var renderer = debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : '';
      var vendor = debugInfo ? gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL) : '';
      var maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE);
      var maxVP = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
      var exts = (gl.getSupportedExtensions() || []).join(',');
      return renderer + '|' + vendor + '|' + maxTex + '|' + (maxVP ? maxVP[0] + 'x' + maxVP[1] : '') + '|' + _fnv1aHash(exts);
    } catch (e) {
      return '';
    }
  }

  function _getAudioFingerprint() {
    return new Promise(function(resolve) {
      try {
        var AudioCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
        if (!AudioCtx) { resolve(''); return; }
        var ctx = new AudioCtx(1, 4500, 44100);
        var osc = ctx.createOscillator();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(10000, ctx.currentTime);
        var comp = ctx.createDynamicsCompressor();
        comp.threshold.setValueAtTime(-50, ctx.currentTime);
        comp.knee.setValueAtTime(40, ctx.currentTime);
        comp.ratio.setValueAtTime(12, ctx.currentTime);
        comp.attack.setValueAtTime(0, ctx.currentTime);
        comp.release.setValueAtTime(0.25, ctx.currentTime);
        osc.connect(comp);
        comp.connect(ctx.destination);
        osc.start(0);
        ctx.startRendering().then(function(buffer) {
          var data = buffer.getChannelData(0);
          var sum = 0;
          for (var i = 4000; i < 4500; i++) {
            sum += Math.abs(data[i]);
          }
          resolve(sum.toFixed(6));
        }).catch(function() {
          resolve('');
        });
      } catch (e) {
        resolve('');
      }
    });
  }

  function _getNavigatorFingerprint() {
    var nav = navigator;
    var parts = [
      'nc:' + (nav.hardwareConcurrency || 0),
      'dm:' + (nav.deviceMemory || 0),
      'mt:' + (nav.maxTouchPoints || 0),
      'pl:' + (nav.platform || ''),
      'la:' + ((nav.languages && nav.languages.join(',')) || nav.language || '')
    ];
    return parts.join('|');
  }

  function _getScreenFingerprint() {
    var s = screen;
    return [
      s.colorDepth || 0,
      s.pixelDepth || 0,
      (window.devicePixelRatio || 1).toFixed(2),
      s.availWidth || 0,
      s.availHeight || 0
    ].join('|');
  }

  function _getMathFingerprint() {
    try {
      return [
        Math.tan(-1e300).toString().length,
        Math.sin(1).toFixed(10),
        Math.atan2(1, 1).toFixed(10)
      ].join('|');
    } catch (e) {
      return '';
    }
  }

  // Session management
  function getOrCreateSession() {
    try {
      var stored = sessionStorage.getItem(SESSION_KEY);
      if (stored) {
        var parsed = JSON.parse(stored);
        if (Date.now() - parsed.lastActivity < SESSION_TIMEOUT_MS) {
          parsed.lastActivity = Date.now();
          sessionStorage.setItem(SESSION_KEY, JSON.stringify(parsed));
          return parsed.id;
        }
      }
      var id = generateUUID();
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({ id: id, lastActivity: Date.now() }));
      return id;
    } catch (e) {
      // sessionStorage unavailable (e.g. sandboxed iframe) — generate ephemeral ID
      return generateUUID();
    }
  }

  // Helper functions
  function generateUUID() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
      var r = Math.random() * 16 | 0;
      var v = c === 'x' ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }

  function getStoredData() {
    try {
      var data = localStorage.getItem(STORAGE_KEY);
      return data ? JSON.parse(data) : {};
    } catch (e) {
      return {};
    }
  }

  function setStoredData(data) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {
      console.warn('Versya: Unable to save to localStorage');
    }
  }

  function getAnonymousId() {
    var stored = getStoredData();
    if (!stored.anonymousId) {
      stored.anonymousId = generateUUID();
      setStoredData(stored);
    }
    return stored.anonymousId;
  }

  function getUserId() {
    var stored = getStoredData();
    return stored.userId || null;
  }

  function setUserId(userId) {
    var stored = getStoredData();
    stored.userId = userId;
    setStoredData(stored);
  }

  function clearUser() {
    var stored = getStoredData();
    delete stored.userId;
    delete stored.userTraits;
    // Keep anonymous ID
    setStoredData(stored);
  }

  function getUserTraits() {
    var stored = getStoredData();
    return stored.userTraits || {};
  }

  function setUserTraits(traits) {
    var stored = getStoredData();
    stored.userTraits = Object.assign({}, stored.userTraits || {}, traits);
    setStoredData(stored);
  }

  function getConsent() {
    try {
      var data = localStorage.getItem(CONSENT_KEY);
      return data ? JSON.parse(data) : { marketing: false, analytics: false };
    } catch (e) {
      return { marketing: false, analytics: false };
    }
  }

  function setConsent(consent) {
    try {
      localStorage.setItem(CONSENT_KEY, JSON.stringify(consent));
    } catch (e) {
      console.warn('Versya: Unable to save consent to localStorage');
    }
  }

  function isInstallVerified(writeKey) {
    try {
      var key = INSTALL_VERIFIED_KEY + '_' + writeKey.substring(0, 12);
      return localStorage.getItem(key) === 'true';
    } catch (e) {
      return false;
    }
  }

  function setInstallVerified(writeKey) {
    try {
      var key = INSTALL_VERIFIED_KEY + '_' + writeKey.substring(0, 12);
      localStorage.setItem(key, 'true');
    } catch (e) {
      // Ignore localStorage errors
    }
  }

  function getContext() {
    var timezone = null;
    try {
      timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || null;
    } catch (e) {
      // Older browsers may not expose an IANA timezone.
    }
    return {
      page: {
        path: window.location.pathname,
        url: window.location.href,
        title: document.title,
        referrer: document.referrer
      },
      userAgent: navigator.userAgent,
      locale: navigator.language,
      timezone: timezone,
      utc_offset_minutes: -new Date().getTimezoneOffset(),
      observed_at: new Date().toISOString(),
      screen: {
        width: screen.width,
        height: screen.height
      },
      attribution: collectAttribution()
    };
  }

  function getCookieValue(name) {
    var cookies = document.cookie ? document.cookie.split(';') : [];
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf(name + '=') === 0) {
        return decodeURIComponent(c.substring(name.length + 1));
      }
    }
    return null;
  }

  function setCookieValue(name, value, maxAge, domain) {
    if (!name || !value) return;
    var cookie = name + '=' + encodeURIComponent(value) + '; path=/; max-age=' + maxAge + '; SameSite=Lax';
    if (domain) cookie += '; domain=' + domain;
    if (window.location.protocol === 'https:') cookie += '; Secure';
    document.cookie = cookie;
  }

  function getStoredClickIds() {
    try {
      var stored = localStorage.getItem('versya_click_ids');
      var parsed = stored ? JSON.parse(stored) : {};
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  function setStoredClickIds(clickIds) {
    try {
      localStorage.setItem('versya_click_ids', JSON.stringify(clickIds || {}));
    } catch (e) {
      // Ignore storage failures; cookies still carry the fallback.
    }
  }

  function clickRecord(value, capturedAt, expiresAt, source) {
    return {
      value: value,
      captured_at: capturedAt,
      expires_at: expiresAt,
      source: source
    };
  }

  function normalizeClickRecord(raw, fallbackValue, nowIso, expiresIso, source) {
    if (raw && typeof raw === 'object' && raw.value) {
      return clickRecord(
        String(raw.value),
        raw.captured_at || nowIso,
        raw.expires_at || expiresIso,
        raw.source || source || 'legacy'
      );
    }
    if (typeof raw === 'string' && raw) {
      return clickRecord(raw, nowIso, expiresIso, 'local_storage');
    }
    if (fallbackValue) {
      return clickRecord(fallbackValue, nowIso, expiresIso, source || 'cookie');
    }
    return null;
  }

  function isClickRecordValid(record, nowMs) {
    if (!record || !record.value) return false;
    var expiresMs = Date.parse(record.expires_at || '');
    return !isNaN(expiresMs) && expiresMs > nowMs;
  }

  function deriveFbc(fbclid, capturedAt) {
    var capturedMs = Date.parse(capturedAt || '') || Date.now();
    return 'fb.1.' + capturedMs + '.' + fbclid;
  }

  function collectAttribution() {
    var params = new URLSearchParams(window.location.search || '');
    var utmKeys = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
    var clickKeys = ['gclid', 'gbraid', 'wbraid', 'fbclid', 'ttclid', 'li_fat_id', 'msclkid', 'twclid', 'epik'];
    var cookieKeys = [
      '_fbp', '_fbc', '_ga', '_gcl_aw', '_gcl_gb', '_ttp', 'ttp',
      '_vsya_gclid', '_vsya_gbraid', '_vsya_wbraid', '_vsya_fbclid',
      '_vsya_ttclid', '_vsya_li_fat_id', '_vsya_msclkid', '_vsya_twclid', '_vsya_epik'
    ];
    var storedClickIds = getStoredClickIds();
    var nowMs = Date.now();
    var nowIso = new Date(nowMs).toISOString();
    var expiresIso = new Date(nowMs + (CLICK_COOKIE_MAX_AGE * 1000)).toISOString();
    var rootDomain = _detectRootDomain();
    var attribution = { utm: {}, click_ids: {}, click_meta: {}, cookies: {}, page: {
      path: window.location.pathname,
      url: window.location.href,
      title: document.title,
      referrer: document.referrer
    }};

    utmKeys.forEach(function(key) {
      var value = params.get(key);
      if (value) attribution.utm[key] = value;
    });

    clickKeys.forEach(function(key) {
      var urlValue = params.get(key);
      var cookieValue = getCookieValue('_vsya_' + key);
      var storedRecord = normalizeClickRecord(
        storedClickIds[key],
        null,
        nowIso,
        expiresIso,
        'local_storage'
      );
      var record;
      if (urlValue) {
        if (storedRecord && storedRecord.value === urlValue) {
          record = storedRecord;
        } else {
          record = clickRecord(urlValue, nowIso, expiresIso, 'url');
          setCookieValue('_vsya_' + key, urlValue, CLICK_COOKIE_MAX_AGE, rootDomain);
        }
      } else {
        record = storedRecord || normalizeClickRecord(
          null,
          cookieValue,
          nowIso,
          expiresIso,
          cookieValue ? 'cookie' : 'local_storage'
        );
        if (record && !storedClickIds[key] && cookieValue) {
          setCookieValue('_vsya_' + key, cookieValue, CLICK_COOKIE_MAX_AGE, rootDomain);
        }
      }
      if (record) storedClickIds[key] = record;
      if (isClickRecordValid(record, nowMs)) {
        attribution.click_ids[key] = record.value;
        attribution.click_meta[key] = {
          captured_at: record.captured_at,
          expires_at: record.expires_at,
          source: record.source
        };
      }
    });

    var fbcValue = getCookieValue('_fbc');
    var storedFbcRecord = normalizeClickRecord(
      storedClickIds._fbc,
      null,
      nowIso,
      expiresIso,
      'local_storage'
    );
    var fbcRecord = storedFbcRecord;
    var currentFbclid = params.get('fbclid');
    if (currentFbclid && attribution.click_ids.fbclid === currentFbclid) {
      var fbclidMeta = attribution.click_meta.fbclid;
      var matchingCookieFbc = fbcValue && fbcValue.slice(-(currentFbclid.length + 1)) === '.' + currentFbclid;
      var currentFbcValue = matchingCookieFbc
        ? fbcValue
        : deriveFbc(currentFbclid, fbclidMeta.captured_at);
      if (!storedFbcRecord || storedFbcRecord.value !== currentFbcValue) {
        fbcRecord = clickRecord(
          currentFbcValue,
          fbclidMeta.captured_at,
          fbclidMeta.expires_at,
          fbclidMeta.source
        );
      }
      if (fbcValue !== currentFbcValue) {
        setCookieValue('_fbc', currentFbcValue, CLICK_COOKIE_MAX_AGE, rootDomain);
        fbcValue = currentFbcValue;
      }
    } else if (fbcValue && (!storedFbcRecord || storedFbcRecord.value !== fbcValue)) {
      fbcRecord = clickRecord(fbcValue, nowIso, expiresIso, 'cookie');
    }
    if (isClickRecordValid(fbcRecord, nowMs)) {
      storedClickIds._fbc = fbcRecord;
      attribution.click_meta._fbc = {
        captured_at: fbcRecord.captured_at,
        expires_at: fbcRecord.expires_at,
        source: fbcRecord.source
      };
    } else if (fbcRecord) {
      storedClickIds._fbc = fbcRecord;
    }
    setStoredClickIds(storedClickIds);

    cookieKeys.forEach(function(key) {
      var value = getCookieValue(key);
      if (value) attribution.cookies[key] = value;
    });

    if (!Object.keys(attribution.utm).length) delete attribution.utm;
    if (!Object.keys(attribution.click_ids).length) delete attribution.click_ids;
    if (!Object.keys(attribution.click_meta).length) delete attribution.click_meta;
    if (!Object.keys(attribution.cookies).length) delete attribution.cookies;
    return attribution;
  }

  // Main SDK Class
  function VersyaMessaging() {
    this.version = VERSION;
    this.writeKey = null;
    this.apiUrl = null;
    this.initialized = false;
    this.queue = [];
    this.debug = false;
  }

  VersyaMessaging.prototype.init = function(writeKey, options) {
    options = options || {};

    if (!writeKey) {
      console.error('Versya: Write key is required');
      return;
    }

    this.writeKey = writeKey;
    this.apiUrl = options.apiUrl || this._detectApiUrl();
    this.debug = options.debug || false;
    this.cookieDomain = options.cookieDomain || _detectRootDomain();
    this._enableDataLayerPush = options.dataLayerPush === true;
    this.initialized = true;
    this._spaTrackingActive = false;
    this._lastTrackedUrl = null;

    // Sync anonymous ID between cookie and localStorage
    this._syncAnonymousId();

    // Compute device fingerprint (async, non-blocking)
    this._deviceFingerprint = null;
    var self = this;
    this._fingerprintReady = this._computeDeviceFingerprint().then(function(fp) {
      self._deviceFingerprint = fp;
      // Attempt recovery if cookies were cleared
      self._attemptFingerprintRecovery();
      return fp;
    });

    this._log('Initialized with write key:', writeKey.substring(0, 12) + '...');

    function startAfterConfig() {
      // Verify installation with backend (only once per write key)
      self._verifyInstall();

      // Check for SDK updates (once per browser session)
      self._checkVersion();

      // Process queued calls
      self._processQueue();

      // Auto-fire initial page view
      self._lastTrackedUrl = window.location.href;
      self.page();

      // Setup extension bridge
      self._setupExtensionBridge();
    }

    // Load remote config before firing the initial page view. If this write
    // key has a tracking domain, the first emitted event can use that proxy.
    this._loadConfig().then(function(config) {
      self._applyConfig(config);
      startAfterConfig();
    }).catch(function(err) {
      self._log('Config load failed:', err.message);
      startAfterConfig();
    });
  };

  VersyaMessaging.prototype._verifyInstall = function() {
    var self = this;

    // Skip if already verified for this write key
    if (isInstallVerified(this.writeKey)) {
      this._log('Installation already verified');
      return;
    }

    var xhr = new XMLHttpRequest();
    xhr.open('POST', this.apiUrl + '/api/v1/verify-install', true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('X-Write-Key', this.writeKey);
    xhr.setRequestHeader('X-SDK-Version', VERSION);

    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        if (xhr.status >= 200 && xhr.status < 300) {
          setInstallVerified(self.writeKey);
          self._log('Installation verified successfully');
        } else {
          self._log('Installation verification failed:', xhr.status);
        }
      }
    };

    xhr.send(JSON.stringify({
      url: window.location.href,
      timestamp: new Date().toISOString()
    }));
  };

  VersyaMessaging.prototype._detectApiUrl = function() {
    // Try to detect API URL from script src
    var scripts = document.getElementsByTagName('script');
    for (var i = 0; i < scripts.length; i++) {
      var src = scripts[i].src;
      if (src && src.indexOf('versya-messaging.js') !== -1) {
        // Extract base URL from script src
        var url = new URL(src);
        return url.origin;
      }
    }
    // Default fallback
    return window.location.origin;
  };

  VersyaMessaging.prototype._log = function() {
    if (this.debug) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift('[Versya]');
      console.log.apply(console, args);
    }
  };

  VersyaMessaging.prototype._syncAnonymousId = function() {
    var stored = getStoredData();
    var lsId = stored.anonymousId || null;
    var cookieId = getCookieVisitorId();

    if (lsId && cookieId) {
      if (lsId !== cookieId) {
        // localStorage wins — update cookie
        setCookieVisitorId(lsId, this.cookieDomain);
        this._log('Synced cookie to localStorage anonymous ID');
      }
    } else if (cookieId && !lsId) {
      // Arriving on new subdomain — adopt cookie
      stored.anonymousId = cookieId;
      setStoredData(stored);
      this._log('Adopted anonymous ID from cookie');
    } else if (lsId && !cookieId) {
      // Upgrade — write to cookie
      setCookieVisitorId(lsId, this.cookieDomain);
      this._log('Wrote anonymous ID to cookie');
    } else {
      // Neither exists — generate new
      var newId = generateUUID();
      stored.anonymousId = newId;
      setStoredData(stored);
      setCookieVisitorId(newId, this.cookieDomain);
      this._log('Generated new anonymous ID');
    }
  };

  // ==========================================
  // Device Fingerprint Computation & Recovery
  // ==========================================

  VersyaMessaging.prototype._computeDeviceFingerprint = function() {
    var self = this;
    return new Promise(function(resolve) {
      // Consent gate — skip if no analytics consent
      try {
        var consent = JSON.parse(localStorage.getItem(CONSENT_KEY) || '{}');
        if (consent.analytics === false) {
          self._log('Fingerprint: Skipped (no analytics consent)');
          resolve(null);
          return;
        }
      } catch (e) { /* proceed if consent check fails */ }

      // Check cache
      try {
        var cached = localStorage.getItem(FINGERPRINT_KEY);
        if (cached) {
          self._log('Fingerprint: Using cached value:', cached);
          resolve(cached);
          return;
        }
      } catch (e) { /* proceed if localStorage fails */ }

      // Collect sync signals
      var signals = [
        'nav:' + _getNavigatorFingerprint(),
        'scr:' + _getScreenFingerprint(),
        'tz:' + (Intl.DateTimeFormat().resolvedOptions().timeZone || '') + '|' + (navigator.language || ''),
        'cv:' + _getCanvasFingerprint(),
        'gl:' + _getWebGLFingerprint(),
        'ma:' + _getMathFingerprint()
      ];

      // Collect async audio signal
      _getAudioFingerprint().then(function(audioFp) {
        signals.push('au:' + audioFp);
        var fp = _fnv1aHash(signals.join('::'));
        self._log('Fingerprint computed:', fp);
        try { localStorage.setItem(FINGERPRINT_KEY, fp); } catch (e) {}
        resolve(fp);
      });
    });
  };

  VersyaMessaging.prototype._attemptFingerprintRecovery = function() {
    var self = this;
    if (!this._deviceFingerprint) return;

    var stored = getStoredData();
    // Only attempt recovery if this is a fresh anonymous_id (cache was cleared)
    if (stored._fpRecoveryAttempted) return;

    // Mark as attempted to prevent retries
    stored._fpRecoveryAttempted = true;
    setStoredData(stored);

    var xhr = new XMLHttpRequest();
    xhr.open('GET', this.apiUrl + '/api/v1/recover-identity?device_fingerprint=' + encodeURIComponent(this._deviceFingerprint), true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-Write-Key', this.writeKey);
    xhr.setRequestHeader('X-SDK-Version', VERSION);

    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4 && xhr.status >= 200 && xhr.status < 300) {
        try {
          var resp = JSON.parse(xhr.responseText);
          if (resp.anonymous_id && resp.anonymous_id !== getAnonymousId()) {
            self._log('Fingerprint recovery: Restored anonymous_id:', resp.anonymous_id);
            var s = getStoredData();
            s.anonymousId = resp.anonymous_id;
            setStoredData(s);
            setCookieVisitorId(resp.anonymous_id, self.cookieDomain);
          }
        } catch (e) {
          self._log('Fingerprint recovery: Parse error');
        }
      }
    };

    xhr.send();
  };

  VersyaMessaging.prototype._processQueue = function() {
    var existingQueue = window.versya && window.versya._q ? window.versya._q : [];

    for (var i = 0; i < existingQueue.length; i++) {
      var call = existingQueue[i];
      var method = call[0];
      var args = call[1];

      if (method === 'init') {
        // Already initialized, skip
        continue;
      }

      if (this[method]) {
        this[method].apply(this, args);
      }
    }
  };

  VersyaMessaging.prototype._request = function(endpoint, data, callback) {
    var self = this;

    if (!this.initialized) {
      this.queue.push({ endpoint: endpoint, data: data, callback: callback });
      return;
    }

    var xhr = new XMLHttpRequest();
    xhr.open('POST', this.apiUrl + '/api/v1' + endpoint, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('X-Write-Key', this.writeKey);
    xhr.setRequestHeader('X-SDK-Version', VERSION);

    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        var success = xhr.status >= 200 && xhr.status < 300;
        var response = null;

        try {
          response = JSON.parse(xhr.responseText);
        } catch (e) {
          response = xhr.responseText;
        }

        if (success) {
          self._log('Request successful:', endpoint, response);
        } else {
          self._log('Request failed:', endpoint, xhr.status, response);
        }

        if (callback) {
          callback(success ? null : new Error(xhr.statusText), response);
        }
      }
    };

    xhr.send(JSON.stringify(data));
  };

  /**
   * Identify a user
   *
   * Supports two call signatures:
   *   identify(userId, traits, callback)          — classic form
   *   identify({contact_id, account_id, traits})  — object form (sends account_id)
   *
   * @param {string|object} userIdOrOptions - User ID string or options object
   * @param {object}  [traits]   - User properties (email, name, phone, etc.)
   * @param {function} [callback] - Optional callback
   */
  VersyaMessaging.prototype.identify = function(userIdOrPayload, traits, callback) {
    var payload;
    if (typeof userIdOrPayload === 'object' && userIdOrPayload !== null) {
        payload = {
            user_id: userIdOrPayload.contact_id || userIdOrPayload.user_id,
            account_id: userIdOrPayload.account_id || null,
            traits: userIdOrPayload.traits || {},
            anonymous_id: getAnonymousId(),
            context: getContext(),
            device_fingerprint: this._deviceFingerprint || null
        };
        callback = traits;
    } else {
        payload = {
            user_id: userIdOrPayload,
            traits: traits || {},
            anonymous_id: getAnonymousId(),
            context: getContext(),
            device_fingerprint: this._deviceFingerprint || null
        };
    }

    if (!payload.user_id) {
      console.error('Versya: User ID (or contact_id) is required for identify');
      return;
    }

    setUserId(payload.user_id);
    setUserTraits(payload.traits);

    this._log('Identify:', payload.user_id, payload.traits, payload.account_id ? '(account: ' + payload.account_id + ')' : '');
    this._request('/identify', payload, callback);
  };

  /**
   * Track an event
   * @param {string} eventName - Name of the event
   * @param {object} properties - Event properties
   * @param {function} callback - Optional callback
   */
  VersyaMessaging.prototype.track = function(eventName, properties, callback) {
    if (typeof properties === 'function') {
      callback = properties;
      properties = {};
    }

    properties = Object.assign({}, properties || {});
    var skipDataLayer = properties.__versyaSkipDataLayer === true;
    if (skipDataLayer) delete properties.__versyaSkipDataLayer;

    if (!eventName) {
      console.error('Versya: Event name is required for track');
      return;
    }

    var eventId = properties.event_id || properties.external_event_id || generateUUID();
    properties.event_id = eventId;
    var context = getContext();
    context.event_id = eventId;

    var payload = {
      event: eventName,
      event_id: eventId,
      user_id: getUserId(),
      anonymous_id: getAnonymousId(),
      properties: properties,
      device_fingerprint: this._deviceFingerprint || null,
      session_id: getOrCreateSession(),
      client_ts: new Date().toISOString(),
      context: context
    };

    this._log('Track:', eventName, properties);
    if (!skipDataLayer) {
      this._pushToDataLayer(eventName, properties, eventId);
    }
    this._request('/track', payload, callback);
  };

  /**
   * Track a page view
   * @param {string} pageName - Optional page name
   * @param {object} properties - Optional page properties
   * @param {function} callback - Optional callback
   */
  VersyaMessaging.prototype.page = function(pageName, properties, callback) {
    if (typeof pageName === 'object') {
      callback = properties;
      properties = pageName;
      pageName = null;
    }

    if (typeof properties === 'function') {
      callback = properties;
      properties = {};
    }

    properties = properties || {};

    var pageProps = Object.assign({}, properties, {
      name: pageName || document.title,
      path: window.location.pathname,
      url: window.location.href,
      title: document.title,
      referrer: document.referrer
    });

    this.track('page_view', pageProps, callback);
  };

  /**
   * Reset/clear the current user
   * Call this on logout — generates a fresh anonymous ID so subsequent
   * events are not linked to the previous visitor.
   */
  VersyaMessaging.prototype.reset = function() {
    this._log('Reset: Generating new anonymous ID');
    var stored = getStoredData();
    delete stored.userId;
    delete stored.userTraits;
    delete stored._fpRecoveryAttempted;
    stored.anonymousId = generateUUID();
    setStoredData(stored);
    setCookieVisitorId(stored.anonymousId, this.cookieDomain);
    // Clear fingerprint to prevent cross-user leakage on shared devices
    this._deviceFingerprint = null;
    try { localStorage.removeItem(FINGERPRINT_KEY); } catch (e) {}
  };

  /**
   * Get the current anonymous ID
   * @returns {string} Anonymous ID
   */
  VersyaMessaging.prototype.getAnonymousId = function() {
    return getAnonymousId();
  };

  /**
   * Get the current user ID
   * @returns {string|null} User ID or null if not identified
   */
  VersyaMessaging.prototype.getUserId = function() {
    return getUserId();
  };

  /**
   * Enable or disable debug mode
   * @param {boolean} enabled - Whether to enable debug mode
   */
  VersyaMessaging.prototype.setDebug = function(enabled) {
    this.debug = enabled;
  };

  /**
   * Print styled diagnostics banner to the console.
   * Always prints (not gated by debug) since explicitly invoked.
   */
  VersyaMessaging.prototype.info = function() {
    var styles = 'background:#6366f1;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold';
    var labelStyle = 'color:#6366f1;font-weight:bold';
    var valStyle = 'color:#333';

    console.log('%c Versya SDK %c v' + VERSION, styles, 'font-size:14px;font-weight:bold;color:#6366f1');
    console.log('%cWrite Key: %c' + (this.writeKey ? this.writeKey.substring(0, 12) + '...' : '(not set)'), labelStyle, valStyle);
    console.log('%cAPI URL:   %c' + (this.apiUrl || '(not set)'), labelStyle, valStyle);
    console.log('%cAnon ID:   %c' + getAnonymousId(), labelStyle, valStyle);
    console.log('%cUser ID:   %c' + (getUserId() || '(none)'), labelStyle, valStyle);
    console.log('%cInit:      %c' + this.initialized, labelStyle, valStyle);
    console.log('%cDebug:     %c' + this.debug, labelStyle, valStyle);
  };

  /**
   * Check if a newer SDK version is available.
   * Throttled to once per browser session via sessionStorage.
   * Non-blocking, fire-and-forget.
   */
  VersyaMessaging.prototype._checkVersion = function() {
    // Throttle: once per browser session
    try {
      if (sessionStorage.getItem(VERSION_CHECK_KEY)) return;
    } catch (e) {
      return; // sessionStorage unavailable
    }

    var self = this;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', this.apiUrl + '/sdk/version', true);
    xhr.withCredentials = true;

    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        try {
          sessionStorage.setItem(VERSION_CHECK_KEY, '1');
        } catch (e) {
          // ignore
        }

        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            var data = JSON.parse(xhr.responseText);
            if (data.version && data.version !== VERSION) {
              console.warn(
                '%c Versya SDK %c Update available: v' + VERSION + ' → v' + data.version +
                '. Regenerate your snippet or clear browser cache.',
                'background:#f59e0b;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold',
                'color:#92400e;font-weight:normal'
              );
            } else {
              self._log('SDK version is up to date (' + VERSION + ')');
            }
          } catch (e) {
            // ignore parse errors
          }
        }
      }
    };

    xhr.send();
  };

  /**
   * Set user consent preferences
   * @param {object} options - Consent options (marketing, analytics)
   * @param {function} callback - Optional callback
   */
  VersyaMessaging.prototype.setConsent = function(options, callback) {
    if (typeof options === 'function') {
      callback = options;
      options = {};
    }

    options = options || {};

    var consent = {
      marketing: options.marketing || false,
      analytics: options.analytics || false
    };

    // Store locally
    setConsent(consent);

    // Send to server
    var payload = {
      marketing: consent.marketing,
      analytics: consent.analytics,
      user_id: getUserId()
    };

    this._log('Set consent:', consent);
    this._request('/consent', payload, function(err, response) {
      if (!err && response && response.consent) {
        setConsent(response.consent);
      }
      if (callback) callback(err, response);
    });

    return consent;
  };

  /**
   * Get current consent state
   * @returns {object} Current consent state
   */
  VersyaMessaging.prototype.getConsent = function() {
    return getConsent();
  };

  /**
   * Fetch personalized visitor data from the server
   * @param {function} callback - Callback(err, data) with visitor traits and scores
   */
  VersyaMessaging.prototype.getVisitorData = function(callback) {
    var self = this;

    if (!this.initialized) {
      if (callback) callback(new Error('SDK not initialized'));
      return;
    }

    var anonymousId = getAnonymousId();
    if (!anonymousId) {
      if (callback) callback(new Error('No anonymous ID'));
      return;
    }

    var xhr = new XMLHttpRequest();
    var url = this.apiUrl + '/api/v1/visitor-data?anonymous_id=' + encodeURIComponent(anonymousId);
    xhr.open('GET', url, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-Write-Key', this.writeKey);
    xhr.setRequestHeader('X-SDK-Version', VERSION);

    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        var success = xhr.status >= 200 && xhr.status < 300;
        var response = null;

        try {
          response = JSON.parse(xhr.responseText);
        } catch (e) {
          response = null;
        }

        if (success) {
          self._log('Visitor data retrieved:', response);
        } else {
          self._log('Visitor data request failed:', xhr.status);
        }

        if (callback) {
          callback(success ? null : new Error(xhr.statusText || 'Request failed'), response);
        }
      }
    };

    xhr.send();
  };

  /**
   * Alias/merge anonymous profile to identified user
   * Call this after identify() to merge anonymous activity
   * @param {string} previousId - Anonymous ID to merge from (optional, uses current anonymous ID)
   * @param {function} callback - Optional callback
   */
  VersyaMessaging.prototype.alias = function(previousId, callback) {
    if (typeof previousId === 'function') {
      callback = previousId;
      previousId = null;
    }

    var anonId = previousId || getAnonymousId();
    var userId = getUserId();

    if (!userId) {
      this._log('Alias: No user ID set, call identify() first');
      if (callback) callback(new Error('No user ID set'));
      return;
    }

    var payload = {
      previous_id: anonId,
      user_id: userId
    };

    this._log('Alias:', anonId, '->', userId);
    this._request('/alias', payload, callback);
  };

  /**
   * Load runtime config from server (for no-code features)
   * @returns {Promise} Resolves with config object
   */
  VersyaMessaging.prototype._loadConfig = function(forceReload) {
    var self = this;
    var cacheKey = CONFIG_KEY + '_' + this.writeKey.substring(0, 12);
    var tsKey = CONFIG_TS_KEY + '_' + this.writeKey.substring(0, 12);

    return new Promise(function(resolve, reject) {
      // Check cache (skip if forceReload)
      if (!forceReload) {
        try {
          var cached = localStorage.getItem(cacheKey);
          var timestamp = localStorage.getItem(tsKey);
          var isFresh = timestamp && (Date.now() - parseInt(timestamp)) < CONFIG_TTL;

          if (cached && isFresh) {
            self._log('Using cached config');
            // Refresh in background
            self._fetchConfigInBackground();
            resolve(JSON.parse(cached));
            return;
          }
        } catch (e) {
          // Continue to fetch
        }
      }

      // Fetch fresh config
      self._fetchConfig().then(resolve).catch(function(err) {
        // Try to use stale cache
        try {
          var stale = localStorage.getItem(cacheKey);
          if (stale) {
            self._log('Using stale cached config');
            resolve(JSON.parse(stale));
            return;
          }
        } catch (e) {
          // No cache available
        }
        resolve({}); // Return empty config as fallback
      });
    });
  };

  /**
   * Fetch config from server
   * @returns {Promise} Resolves with config object
   */
  VersyaMessaging.prototype._fetchConfig = function() {
    var self = this;
    var cacheKey = CONFIG_KEY + '_' + this.writeKey.substring(0, 12);
    var tsKey = CONFIG_TS_KEY + '_' + this.writeKey.substring(0, 12);

    return new Promise(function(resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', self.apiUrl + '/api/v1/config', true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-Write-Key', self.writeKey);
      xhr.setRequestHeader('X-SDK-Version', VERSION);

      xhr.onreadystatechange = function() {
        if (xhr.readyState === 4) {
          if (xhr.status >= 200 && xhr.status < 300) {
            try {
              var config = JSON.parse(xhr.responseText);
              // Cache it
              localStorage.setItem(cacheKey, JSON.stringify(config));
              localStorage.setItem(tsKey, Date.now().toString());
              self._log('Config fetched and cached');
              resolve(config);
            } catch (e) {
              reject(e);
            }
          } else {
            reject(new Error('Config fetch failed: ' + xhr.status));
          }
        }
      };

      xhr.send();
    });
  };

  /**
   * Fetch config in background without blocking
   */
  VersyaMessaging.prototype._fetchConfigInBackground = function() {
    var self = this;
    setTimeout(function() {
      self._fetchConfig().then(function(config) {
        self._applyConfig(config);
      }).catch(function(err) {
        self._log('Background config refresh failed:', err.message);
      });
    }, 100);
  };

  /**
   * Apply loaded config (no-code mappings, dataLayer settings)
   * @param {object} config - Config object from server
   */
  VersyaMessaging.prototype._applyConfig = function(config) {
    if (!config) return;

    this._config = config;
    this._log('Applying config:', config.version || 'unknown version');

    // Prefer the customer-owned tracking domain once it is configured. This
    // lets older API-origin snippets move future events onto the first-party
    // proxy without changing the customer's event instrumentation.
    if (config.tracking && config.tracking.proxyUrl) {
      var proxyUrl = String(config.tracking.proxyUrl).replace(/\/+$/, '');
      if (proxyUrl && proxyUrl !== this.apiUrl) {
        this._log('Switching to first-party tracking proxy:', proxyUrl);
        this.apiUrl = proxyUrl;
      }
    }

    // Setup dataLayer integration
    if (config.dataLayer) {
      if (config.dataLayer.push) {
        this._enableDataLayerPush = true;
      }
      if (config.dataLayer.listen) {
        this._setupDataLayerListener(config.dataLayer.events || '*');
      }
    }

    // Apply VEF-based mappings (attach event listeners)
    if (config.noCodeMappings && config.noCodeMappings.length > 0) {
      this._setupVEFMappings(config.noCodeMappings);
    }

    // Setup SPA auto-tracking if enabled in dashboard
    if (config.settings && config.settings.autoTrack && config.settings.autoTrack.spaPages) {
      this._setupSpaTracking();
    }
  };

  /**
   * Setup SPA page tracking by intercepting pushState/replaceState and popstate.
   * Uses a 300ms debounce to prevent double-fires from redirects.
   */
  VersyaMessaging.prototype._setupSpaTracking = function() {
    if (this._spaTrackingActive) return;
    this._spaTrackingActive = true;

    var self = this;
    var debounceTimer = null;
    var DEBOUNCE_MS = 300;

    function onUrlChange() {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function() {
        var currentUrl = window.location.href;
        if (currentUrl !== self._lastTrackedUrl) {
          self._vefCache = {};
          self._lastTrackedUrl = currentUrl;
          self.page();
          self._log('SPA page tracked:', currentUrl);
        }
      }, DEBOUNCE_MS);
    }

    // Intercept history.pushState
    var originalPushState = history.pushState;
    history.pushState = function() {
      originalPushState.apply(this, arguments);
      onUrlChange();
    };

    // Intercept history.replaceState
    var originalReplaceState = history.replaceState;
    history.replaceState = function() {
      originalReplaceState.apply(this, arguments);
      onUrlChange();
    };

    // Listen for back/forward navigation
    window.addEventListener('popstate', onUrlChange);

    this._log('SPA page tracking enabled');
  };

  /**
   * Push event to dataLayer (for GTM integration)
   * @param {string} eventName - Event name
   * @param {object} properties - Event properties
   */
  VersyaMessaging.prototype._pushToDataLayer = function(eventName, properties, eventId) {
    if (!this._enableDataLayerPush) return;

    window.dataLayer = window.dataLayer || [];
    var data = Object.assign({}, properties, {
      event: eventName,
      event_id: eventId || properties.event_id,
      external_event_id: eventId || properties.external_event_id || properties.event_id,
      _versyaProcessed: true,
      _versyaMirrored: true
    });
    window.dataLayer.push(data);
    this._log('Pushed to dataLayer:', eventName);
  };

  /**
   * Setup dataLayer listener to capture existing GTM events
   * @param {string|array} events - Events to listen for ('*' for all)
   */
  VersyaMessaging.prototype._setupDataLayerListener = function(events) {
    var self = this;
    window.dataLayer = window.dataLayer || [];

    var originalPush = window.dataLayer.push.bind(window.dataLayer);
    window.dataLayer.push = function() {
      var result = originalPush.apply(this, arguments);

      // Forward to Versya
      for (var i = 0; i < arguments.length; i++) {
        var data = arguments[i];
        if (data && data.event && !data._versyaProcessed) {
          // Check if we should capture this event
          if (events === '*' || (Array.isArray(events) && events.indexOf(data.event) !== -1)) {
            data._versyaProcessed = true;
            if (!data.event_id) data.event_id = generateUUID();
            var forwarded = Object.assign({}, data, {
              __versyaSkipDataLayer: true
            });
            self.track(data.event, forwarded);
          }
        }
      }
      return result;
    };

    this._log('DataLayer listener setup for:', events);
  };

  /**
   * Setup VEF-based element mappings (click tracking, etc.)
   * @param {array} mappings - Array of VEF mapping configurations
   */
  VersyaMessaging.prototype._setupVEFMappings = function(mappings) {
    if (!mappings || !mappings.length) return;
    var self = this;

    // Per-mapping failure counters (circuit breaker)
    var failureCounts = {};
    var CIRCUIT_BREAKER_LIMIT = 20;

    mappings.forEach(function(mapping) {
      try {
        // Validate mapping schema
        if (!mapping.mappingUid || !mapping.eventName || !mapping.trigger || !mapping.vef) return;
        if (!mapping.vef.layers || !mapping.vef.layers.length) return;

        // Check page pattern
        if (mapping.pagePattern && !self._matchesPageBounded(mapping.pagePattern, mapping.pageScopeType || 'glob')) return;

        // Check viewport scope
        if (mapping.viewportScope && mapping.viewportScope !== 'all' && !self._matchesViewport(mapping.viewportScope)) return;

        var trigger = mapping.trigger;
        var eventType = (trigger === 'submit') ? 'submit' : (trigger === 'change') ? 'change' : (trigger === 'focus') ? 'focusin' : 'click';

        failureCounts[mapping.mappingUid] = 0;

        var handler = function(e) {
          try {
            // Circuit breaker check
            if (failureCounts[mapping.mappingUid] >= CIRCUIT_BREAKER_LIMIT) return;

            // Use composedPath for Shadow DOM support
            var target = (e.composedPath && e.composedPath()[0]) || e.target;

            // VEF resolution
            if (!self._resolveVEF(target, mapping.vef)) {
              failureCounts[mapping.mappingUid]++;
              return;
            }

            // Extract properties safely
            var properties = {};
            if (mapping.properties && mapping.properties.length) {
              properties = self._extractPropertiesSafe(mapping.properties, target);
            }

            // Check consent if required
            if (mapping.consentRequired) {
              var consent = self._getStoredConsent();
              if (!consent || !consent.analytics) return;
            }

            // Rate limit check
            if (!self._checkRateLimit()) return;

            self.track(mapping.eventName, properties);
          } catch(err) {
            failureCounts[mapping.mappingUid]++;
            if (self.debug) console.warn('[Versya] VEF mapping error:', mapping.mappingUid, err);
          }
        };

        document.addEventListener(eventType, handler, true);
        self._noCodeCleanups = self._noCodeCleanups || [];
        self._noCodeCleanups.push(function() { document.removeEventListener(eventType, handler, true); });
      } catch(err) {
        if (self.debug) console.warn('[Versya] Failed to setup mapping:', mapping.mappingUid, err);
      }
    });

    this._log('VEF mappings setup:', mappings.length, 'mappings');
  };

  /**
   * Check if current page matches pattern
   * @param {string} pattern - Page pattern (e.g., '/product/*')
   * @returns {boolean}
   */
  VersyaMessaging.prototype._matchesPage = function(pattern) {
    var path = window.location.pathname;
    // Convert glob pattern to regex
    var regex = new RegExp('^' + pattern.replace(/\*/g, '.*') + '$');
    return regex.test(path);
  };

  /**
   * Extract properties from DOM based on property definitions
   * @param {array} propertyDefs - Property definitions
   * @returns {object}
   */
  VersyaMessaging.prototype._extractProperties = function(propertyDefs) {
    var props = {};

    propertyDefs.forEach(function(def) {
      var el = document.querySelector(def.selector);
      if (el) {
        var value;
        if (def.extract === 'text') {
          value = el.textContent.trim();
        } else if (def.extract === 'attribute') {
          value = el.getAttribute(def.attribute);
        } else if (def.extract === 'value') {
          value = el.value;
        }

        // Apply transforms
        if (value && def.transform === 'parse_currency') {
          value = parseFloat(value.replace(/[^0-9.,]/g, ''));
        } else if (value && def.transform === 'parse_int') {
          value = parseInt(value.replace(/[^0-9]/g, ''), 10);
        }

        if (value !== undefined && value !== null) {
          props[def.name] = value;
        }
      }
    });

    return props;
  };

  // ── VEF Resolution ──────────────────────────────────────────────

  VersyaMessaging.prototype._vefCache = {};
  VersyaMessaging.prototype._vefCacheTTL = 2000;

  VersyaMessaging.prototype._resolveVEF = function(element, vef) {
    // Check cache
    var cacheKey = vef.vefId;
    var now = Date.now();
    if (this._vefCache[cacheKey] && (now - this._vefCache[cacheKey].time < this._vefCacheTTL)) {
      return this._vefCache[cacheKey].result;
    }

    // Sort layers by confidence (desc) and try each
    var layers = vef.layers.slice().sort(function(a, b) { return b.confidence - a.confidence; });
    for (var i = 0; i < layers.length; i++) {
      if (this._tryVEFLayer(element, layers[i])) {
        this._vefCache[cacheKey] = { result: true, time: now };
        return true;
      }
    }
    this._vefCache[cacheKey] = { result: false, time: now };
    return false;
  };

  VersyaMessaging.prototype._tryVEFLayer = function(element, layer) {
    try {
      switch (layer.strategy) {
        case 'data_attribute':
          // Check data-versya-id on element or ancestors
          var el = element;
          var maxDepth = 10;
          while (el && maxDepth-- > 0) {
            if (el.getAttribute && el.getAttribute('data-versya-id') === layer.value) return true;
            el = el.parentElement;
          }
          return false;

        case 'semantic_stable':
          // Match by stable semantic attributes
          if (!layer.selector) return false;
          var attrs = ['aria-label', 'role', 'name', 'type', 'data-testid', 'href', 'placeholder'];
          for (var i = 0; i < attrs.length; i++) {
            var val = element.getAttribute && element.getAttribute(attrs[i]);
            if (val && val === layer.value) return true;
          }
          // Check non-hashed id
          if (element.id && element.id === layer.value && !/^[a-f0-9]{6,}$/i.test(element.id)) return true;
          return false;

        case 'structural_landmark':
          // Find nearest stable ancestor and match tag path
          if (!layer.selector) return false;
          try {
            var landmark = document.querySelector(layer.selector);
            if (!landmark) return false;
            return landmark.contains(element);
          } catch(e) { return false; }

        case 'text_content':
          // Match by textContent within context
          var context = layer.context ? document.querySelector(layer.context) : document;
          if (!context) return false;
          var candidates = context.querySelectorAll(layer.elementType || '*');
          var matches = [];
          for (var j = 0; j < candidates.length; j++) {
            if (candidates[j].textContent && candidates[j].textContent.trim() === layer.value) matches.push(candidates[j]);
          }
          return matches.length === 1 && matches[0] === element;

        case 'visual_position':
          // Relative position within landmark (last resort)
          if (!layer.selector) return false;
          try {
            var container = document.querySelector(layer.selector);
            if (!container || !container.contains(element)) return false;
            // Check element is interactive before accepting
            var interactive = ['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'];
            if (interactive.indexOf(element.tagName) === -1 && !element.getAttribute('role')) return false;
            return true;
          } catch(e) { return false; }

        default:
          return false;
      }
    } catch(e) {
      return false;
    }
  };

  // ── Viewport Scope Filter ───────────────────────────────────────

  VersyaMessaging.prototype._matchesViewport = function(scope) {
    var w = window.innerWidth || document.documentElement.clientWidth;
    switch (scope) {
      case 'mobile': return w <= 768;
      case 'tablet': return w > 768 && w <= 1024;
      case 'desktop': return w > 1024;
      default: return true;
    }
  };

  // ── Bounded Page Pattern ────────────────────────────────────────

  VersyaMessaging.prototype._matchesPageBounded = function(pattern, scopeType) {
    if (!pattern) return true;
    var path = window.location.pathname;
    try {
      switch (scopeType) {
        case 'exact': return path === pattern;
        case 'starts_with': return path.indexOf(pattern) === 0;
        case 'contains': return path.indexOf(pattern) !== -1;
        case 'glob':
          if (pattern.length > 500) return false;
          // Safe glob: * becomes [^/]*, ** not allowed
          var regex = '^' + pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*') + '$';
          return new RegExp(regex).test(path);
        default: return true;
      }
    } catch(e) {
      return false;
    }
  };

  // ── Safe Property Extraction ────────────────────────────────────

  VersyaMessaging.prototype._extractPropertiesSafe = function(propertyDefs, triggerElement) {
    var props = {};
    for (var i = 0; i < propertyDefs.length; i++) {
      var def = propertyDefs[i];
      try {
        var value = null;

        switch (def.source || def.dataSource) {
          case 'this_element':
            value = this._extractFromElement(triggerElement, def);
            break;
          case 'another_element':
            if (def.selector) {
              var el = document.querySelector(def.selector);
              if (el) value = this._extractFromElement(el, def);
            }
            break;
          case 'window_path':
            value = this._safeWindowPath(def.selector || def.attribute);
            break;
          case 'datalayer':
            value = this._readDataLayer(def.selector || def.attribute);
            break;
          case 'cookie':
            value = this._readCookie(def.selector || def.attribute);
            break;
          case 'localstorage_key':
            try { value = localStorage.getItem(def.selector || def.attribute); } catch(e) {}
            break;
          case 'sessionstorage_key':
            try { value = sessionStorage.getItem(def.selector || def.attribute); } catch(e) {}
            break;
        }

        if (value !== null && value !== undefined) {
          value = this._sanitizePropertyValue(value, def);
          if (value !== null) props[def.name] = value;
        }
      } catch(e) {
        if (this.debug) console.warn('[Versya] Property extraction error:', def.name, e);
      }
    }
    return props;
  };

  VersyaMessaging.prototype._extractFromElement = function(el, def) {
    if (!el) return null;
    switch (def.extract) {
      case 'text_content': return el.textContent ? el.textContent.trim() : null;
      case 'attribute': return el.getAttribute ? el.getAttribute(def.attribute) : null;
      case 'value': return el.value !== undefined ? el.value : null;
      default: return el.textContent ? el.textContent.trim() : null;
    }
  };

  VersyaMessaging.prototype._safeWindowPath = function(path) {
    if (!path || typeof path !== 'string') return null;
    // Blocked keys
    var blocked = ['__proto__', 'constructor', 'prototype'];
    var parts = path.split('.');
    if (parts.length > 5) return null;

    var obj = window;
    for (var i = 0; i < parts.length; i++) {
      if (blocked.indexOf(parts[i]) !== -1) return null;
      if (obj === null || obj === undefined) return null;
      obj = obj[parts[i]];
      if (typeof obj === 'function') return null;
    }
    // Size limit: 10KB
    var str = typeof obj === 'string' ? obj : JSON.stringify(obj);
    if (str && str.length > 10240) return null;
    return obj;
  };

  VersyaMessaging.prototype._readDataLayer = function(key) {
    if (!window.dataLayer || !Array.isArray(window.dataLayer)) return null;
    // Read the latest matching entry
    for (var i = window.dataLayer.length - 1; i >= 0; i--) {
      var entry = window.dataLayer[i];
      if (entry && entry[key] !== undefined) {
        var val = entry[key];
        var str = typeof val === 'string' ? val : JSON.stringify(val);
        if (str && str.length > 10240) return null;
        return val;
      }
    }
    return null;
  };

  VersyaMessaging.prototype._readCookie = function(name) {
    if (!name) return null;
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var parts = cookies[i].trim().split('=');
      if (parts[0] === name) return decodeURIComponent(parts.slice(1).join('='));
    }
    return null;
  };

  VersyaMessaging.prototype._sanitizePropertyValue = function(value, def) {
    if (value === null || value === undefined) return null;
    var str = String(value);
    // Strip control characters
    str = str.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '');
    // Strip HTML tags
    str = str.replace(/<[^>]*>/g, '');
    // Truncate
    var maxLen = def.maxLength || def.max_length || 512;
    if (maxLen > 4096) maxLen = 4096;
    if (str.length > maxLen) str = str.substring(0, maxLen);
    // Normalize Unicode
    if (str.normalize) str = str.normalize('NFC');

    // Transform
    var transform = def.transform || 'none';
    switch (transform) {
      case 'number': var n = parseFloat(str); return isNaN(n) ? null : n;
      case 'currency': return parseFloat(str.replace(/[^0-9.-]/g, '')) || null;
      case 'lowercase': return str.toLowerCase();
      case 'uppercase': return str.toUpperCase();
      case 'trim': return str.trim();
      case 'hash': return null; // Hash done server-side
      default: return str;
    }
  };

  // ── Extension PostMessage Bridge ────────────────────────────────

  VersyaMessaging.prototype._bridgeNonce = null;

  VersyaMessaging.prototype._setupExtensionBridge = function() {
    var self = this;
    var ALLOWED_ACTIONS = ['handshake', 'get_state', 'reload_config'];

    window.addEventListener('message', function(event) {
      if (event.source !== window) return;
      if (!event.data || event.data.type !== 'VERSYA_EXT_REQUEST') return;

      var action = event.data.action;
      if (ALLOWED_ACTIONS.indexOf(action) === -1) return;

      if (action === 'handshake') {
        self._bridgeNonce = event.data.nonce || null;
        window.postMessage({
          type: 'VERSYA_SDK_RESPONSE',
          action: 'handshake',
          nonce: self._bridgeNonce,
          sdkVersion: self.version,
          writeKey: self.writeKey ? self.writeKey.substring(0, 8) + '...' : null,
          initialized: self.initialized || false,
        }, '*');
        return;
      }

      // Validate nonce for non-handshake actions
      if (!self._bridgeNonce || event.data.nonce !== self._bridgeNonce) return;

      if (action === 'get_state') {
        window.postMessage({
          type: 'VERSYA_SDK_RESPONSE',
          action: 'get_state',
          nonce: self._bridgeNonce,
          state: {
            initialized: self.initialized || false,
            writeKey: self.writeKey ? self.writeKey.substring(0, 8) + '...' : null,
            userId: getUserId(),
            anonymousId: getAnonymousId(),
            sdkVersion: self.version,
          },
        }, '*');
      } else if (action === 'reload_config') {
        self.reloadConfig();
        window.postMessage({
          type: 'VERSYA_SDK_RESPONSE',
          action: 'reload_config',
          nonce: self._bridgeNonce,
          status: 'ok',
        }, '*');
      }
    });

    // Invalidate nonce on navigation
    window.addEventListener('beforeunload', function() {
      self._bridgeNonce = null;
    });

    // Broadcast SDK ready
    window.postMessage({ type: 'VERSYA_SDK_READY', sdkVersion: self.version }, '*');
  };

  VersyaMessaging.prototype.reloadConfig = function() {
    var self = this;
    self._loadConfig(true).then(function(config) {
      self._applyConfig(config);
    }).catch(function(err) {
      self._log('Config reload failed:', err.message);
    });
  };

  // ── Rate Limiter ────────────────────────────────────────────────

  VersyaMessaging.prototype._rateLimitTokens = 50;
  VersyaMessaging.prototype._rateLimitMax = 50;
  VersyaMessaging.prototype._rateLimitRate = 20;
  VersyaMessaging.prototype._rateLimitLastRefill = 0;

  VersyaMessaging.prototype._checkRateLimit = function() {
    var now = Date.now();
    if (this._rateLimitLastRefill === 0) this._rateLimitLastRefill = now;
    // Refill tokens
    var elapsed = (now - this._rateLimitLastRefill) / 1000;
    this._rateLimitTokens = Math.min(this._rateLimitMax, this._rateLimitTokens + elapsed * this._rateLimitRate);
    this._rateLimitLastRefill = now;
    if (this._rateLimitTokens < 1) {
      if (this.debug) console.warn('[Versya] Rate limit exceeded, event dropped');
      return false;
    }
    this._rateLimitTokens--;
    return true;
  };

  VersyaMessaging.prototype._getStoredConsent = function() {
    try {
      var data = localStorage.getItem('versya_consent');
      return data ? JSON.parse(data) : null;
    } catch(e) { return null; }
  };

  // Initialize SDK
  var sdk = new VersyaMessaging();

  // Process existing queue if stub was used
  var existingVersya = window.versya;
  if (existingVersya && existingVersya._q && existingVersya._q.length > 0) {
    // Find init call
    for (var i = 0; i < existingVersya._q.length; i++) {
      var call = existingVersya._q[i];
      if (call[0] === 'init') {
        var args = Array.prototype.slice.call(call[1]);
        sdk.init.apply(sdk, args);
        break;
      }
    }

    // Process remaining queue
    for (var j = 0; j < existingVersya._q.length; j++) {
      var queuedCall = existingVersya._q[j];
      if (queuedCall[0] !== 'init' && sdk[queuedCall[0]]) {
        sdk[queuedCall[0]].apply(sdk, Array.prototype.slice.call(queuedCall[1]));
      }
    }
  }

  // Expose SDK globally
  window.versya = sdk;

})(window, document);
