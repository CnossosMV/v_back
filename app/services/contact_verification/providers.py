from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

import httpx


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        transient: bool = True,
        outcome_unknown: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.transient = transient
        # True means the provider may have accepted a mutating request even
        # though the client did not receive a usable response. Callers must
        # reconcile provider state before retrying that mutation.
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class VerificationResult:
    canonical_status: str
    provider_status: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def millionverifier_status(result: str) -> str:
    value = (result or "").strip().lower()
    if value == "ok":
        return "valid"
    if value in {"invalid", "disposable"}:
        return "invalid"
    if value in {"catch_all", "catch-all", "unknown"}:
        return "risky"
    raise ProviderRequestError(f"Unsupported MillionVerifier result: {value or 'empty'}", code="invalid_response", transient=False)


def evolution_status(exists: bool) -> str:
    return "valid" if exists else "invalid"


class MillionVerifierAdapter:
    provider_code = "millionverifier"
    provider_version = "v3/v2"
    key_source = "platform"
    supports_bulk_reconciliation = True

    def __init__(self) -> None:
        self.api_key = os.getenv("MILLION_VERIFIER_API_KEY", "").strip()
        self.single_base_url = os.getenv("MILLIONVERIFIER_API_BASE_URL", "").strip().rstrip("/")
        self.bulk_base_url = os.getenv("MILLIONVERIFIER_BULK_API_BASE_URL", "").strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.single_base_url and self.bulk_base_url)

    def _require(self) -> None:
        if not self.configured:
            raise ProviderConfigurationError(
                "MillionVerifier requires MILLION_VERIFIER_API_KEY, MILLIONVERIFIER_API_BASE_URL and MILLIONVERIFIER_BULK_API_BASE_URL"
            )

    async def credits(self) -> Dict[str, Any]:
        self._require()
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{self.single_base_url}/api/v3/credits", params={"api": self.api_key})
        self._raise(response)
        data = response.json()
        if data.get("error"):
            code = str(data.get("error"))
            raise ProviderRequestError(code, code=code, transient=False)
        return {
            "credits": int(data.get("credits") or 0),
            "bulk_credits": int(data.get("bulk_credits") or 0),
            "renewing_credits": int(data.get("renewing_credits") or 0),
            "plan": data.get("plan"),
        }

    async def verify_single(self, email: str) -> VerificationResult:
        self._require()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.single_base_url}/api/v3/",
                params={"api": self.api_key, "email": email, "timeout": 20},
            )
        self._raise(response)
        data = response.json()
        if data.get("error"):
            code = str(data.get("error"))
            transient = code not in {"invalid_api_key", "insufficient_credits", "ip_address_blocked"}
            raise ProviderRequestError(code, code=code, transient=transient)
        provider_status = str(data.get("result") or "")
        return VerificationResult(
            canonical_status=millionverifier_status(provider_status),
            provider_status=provider_status,
            metadata=self._sanitize(data),
        )

    async def upload_bulk(
        self,
        rows: Iterable[tuple[str, str]],
        *,
        filename: str,
    ) -> Dict[str, Any]:
        self._require()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["email", "versya_item_key"])
        for item_key, email in rows:
            writer.writerow([email, item_key])
        content = stream.getvalue().encode("utf-8")
        files = {"file_contents": (filename, content, "text/csv")}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.bulk_base_url}/bulkapi/v2/upload",
                    params={"key": self.api_key},
                    files=files,
                )
            try:
                self._raise(response)
            except ProviderRequestError as exc:
                if exc.transient:
                    raise ProviderRequestError(
                        str(exc),
                        code=exc.code,
                        transient=True,
                        outcome_unknown=True,
                    ) from exc
                raise
            try:
                data = response.json()
            except (TypeError, ValueError) as exc:
                raise ProviderRequestError(
                    "MillionVerifier bulk upload returned an unreadable response",
                    code="invalid_response",
                    transient=True,
                    outcome_unknown=True,
                ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderRequestError(
                "MillionVerifier bulk upload transport outcome is unknown",
                code="upload_outcome_unknown",
                transient=True,
                outcome_unknown=True,
            ) from exc
        if data.get("error"):
            code = str(data["error"])
            transient = code in {"internal_error"}
            raise ProviderRequestError(
                code,
                code=code,
                transient=transient,
                outcome_unknown=transient,
            )
        if not data.get("file_id"):
            raise ProviderRequestError(
                "MillionVerifier bulk upload returned no file_id",
                code="invalid_response",
                transient=True,
                outcome_unknown=True,
            )
        return data

    async def find_bulk_files(
        self,
        *,
        filename: str,
        createdate_from: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        """Return exact-name matches for a previously persisted upload intent.

        MillionVerifier's documented ``name`` filter is a contains match, so
        exact matching is intentionally repeated locally before an upload is
        adopted as ours.
        """
        self._require()
        params: Dict[str, Any] = {
            "key": self.api_key,
            "name": filename,
            "offset": 0,
            "limit": 50,
        }
        if createdate_from:
            params["createdate_from"] = createdate_from
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.bulk_base_url}/bulkapi/v2/filelist",
                    params=params,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderRequestError(
                "MillionVerifier bulk file reconciliation is unavailable",
                code="reconciliation_transport_error",
                transient=True,
            ) from exc
        self._raise(response)
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise ProviderRequestError(
                "MillionVerifier bulk file list returned an unreadable response",
                code="invalid_response",
                transient=True,
            ) from exc
        if data.get("error"):
            code = str(data["error"])
            raise ProviderRequestError(
                code,
                code=code,
                transient=code in {"internal_error"},
            )
        files = data.get("files")
        if not isinstance(files, list):
            raise ProviderRequestError(
                "MillionVerifier bulk file list omitted files",
                code="invalid_response",
                transient=True,
            )
        return [
            row for row in files
            if isinstance(row, dict) and str(row.get("file_name") or "") == filename
        ]

    async def bulk_info(self, file_id: str) -> Dict[str, Any]:
        self._require()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.bulk_base_url}/bulkapi/v2/fileinfo",
                params={"key": self.api_key, "file_id": file_id},
            )
        self._raise(response)
        data = response.json()
        if data.get("error"):
            code = str(data.get("error"))
            raise ProviderRequestError(code, code=code, transient=code in {"internal_error"})
        return data

    async def download_bulk(self, file_id: str) -> Dict[str, VerificationResult]:
        self._require()
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(
                f"{self.bulk_base_url}/bulkapi/v2/download",
                params={"key": self.api_key, "file_id": file_id, "filter": "all"},
            )
        self._raise(response)
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            data = response.json()
            raise ProviderRequestError(str(data.get("error") or "Bulk report unavailable"), code="report_unavailable")
        text = response.content.decode("utf-8-sig")
        return self._parse_bulk_report(text)

    @staticmethod
    def _parse_bulk_report(text: str) -> Dict[str, VerificationResult]:
        reader = csv.DictReader(io.StringIO(text))
        results: Dict[str, VerificationResult] = {}
        for row in reader:
            normalized = {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items() if k}
            item_key = normalized.get("versya_item_key") or normalized.get("item_key")
            email = normalized.get("email") or normalized.get("email_address")
            status = normalized.get("result") or normalized.get("status") or normalized.get("verification_result")
            result_key = str(item_key) if item_key else (f"email:{email}" if email else None)
            if not result_key or not status:
                continue
            results[result_key] = VerificationResult(
                canonical_status=millionverifier_status(str(status)),
                provider_status=str(status).strip().lower(),
                metadata={
                    key: normalized.get(key)
                    for key in ("quality", "subresult", "free", "role", "resultcode")
                    if normalized.get(key) not in (None, "")
                },
            )
        if not results:
            raise ProviderRequestError("MillionVerifier report did not contain mappable results", code="invalid_report", transient=False)
        return results

    async def stop_bulk(self, file_id: str) -> None:
        self._require()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.bulk_base_url}/bulkapi/stop",
                params={"key": self.api_key, "file_id": file_id},
            )
        self._raise(response)
        data = response.json()
        if data.get("error"):
            raise ProviderRequestError(str(data["error"]), code=str(data["error"]), transient=False)

    async def delete_bulk(self, file_id: str) -> None:
        self._require()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.bulk_base_url}/bulkapi/v2/delete",
                params={"key": self.api_key, "file_id": file_id},
            )
        self._raise(response)
        data = response.json()
        if data.get("error"):
            raise ProviderRequestError(str(data["error"]), code=str(data["error"]), transient=False)

    @staticmethod
    def _sanitize(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: data.get(key)
            for key in ("quality", "resultcode", "subresult", "free", "role", "executiontime", "livemode")
            if data.get(key) is not None
        }

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            transient = response.status_code >= 500 or response.status_code in {408, 429}
            raise ProviderRequestError(
                f"Provider HTTP {response.status_code}",
                code=f"http_{response.status_code}",
                transient=transient,
            ) from exc


class EvolutionWhatsAppAdapter:
    provider_code = "evolution_api"
    provider_version = "whatsappNumbers"
    key_source = "project"

    async def verify(self, instance, db, phone_e164: str) -> VerificationResult:
        from app.services.whatsapp_validation_service import WhatsAppValidationService
        from app.services.evolution_api_service import evolution_api_service

        token = await WhatsAppValidationService._get_instance_token(instance, db)
        result = await evolution_api_service.check_whatsapp_numbers(
            instance_name=instance.instance_name,
            instance_token=token,
            numbers=[phone_e164],
        )
        if not result.get("success"):
            raise ProviderRequestError(str(result.get("error") or "Evolution lookup failed"))
        clean = "".join(ch for ch in phone_e164 if ch.isdigit())
        valid = set(result.get("valid") or [])
        invalid = set(result.get("invalid") or [])
        if clean in valid:
            return VerificationResult(evolution_status(True), "exists", {"exists": True})
        if clean in invalid:
            return VerificationResult(evolution_status(False), "not_found", {"exists": False})
        raise ProviderRequestError("Evolution returned no result for the number", code="unmapped_response", transient=True)


class VerificationProviderRegistry:
    def __init__(self) -> None:
        self._email = {"millionverifier": MillionVerifierAdapter}
        self._whatsapp = {"evolution_api": EvolutionWhatsAppAdapter}

    def email(self, code: str) -> MillionVerifierAdapter:
        factory = self._email.get(code)
        if not factory:
            raise ProviderConfigurationError(f"Unsupported email verification provider: {code}")
        return factory()

    def whatsapp(self, code: str) -> EvolutionWhatsAppAdapter:
        factory = self._whatsapp.get(code)
        if not factory:
            raise ProviderConfigurationError(f"Unsupported WhatsApp verification provider: {code}")
        return factory()


provider_registry = VerificationProviderRegistry()
