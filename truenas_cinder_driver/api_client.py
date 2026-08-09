"""
API client for TrueNAS Scale REST API.

This module provides a robust interface to interact with the TrueNAS Scale
REST API, handling authentication and error responses.

Every failure this client can produce is a :class:`TrueNASAPIError` subclass,
including network-level ones. Callers -- the driver in particular -- can
therefore translate to ``cinder.exception.VolumeBackendAPIException`` with a
single ``except`` clause and never see a raw ``requests`` exception.
"""

import logging
import time
import requests
from typing import Callable, Dict, Any, Optional, List, Tuple, Union
from urllib.parse import quote, urlsplit


LOG = logging.getLogger(__name__)

# TrueNAS reports and accepts volsize in bytes; Cinder works in GB.
GIB = 1024 ** 3

# Every endpoint hangs off this prefix. Held separately so a configured
# base_url may include it or not without producing a doubled path.
API_PREFIX = "/api/v2.0"

# (connect, read), in seconds. A cinder-volume worker is blocked for the whole
# duration of a request, so an unbounded read timeout -- requests' default --
# turns a wedged appliance into a wedged worker with no recovery short of a
# service restart. The read budget is generous because a non-sparse zvol
# create can genuinely take tens of seconds.
DEFAULT_TIMEOUT: Tuple[float, float] = (10.0, 60.0)

# Total attempts, not retries: 3 means one initial call plus two retries.
DEFAULT_MAX_ATTEMPTS = 3

# Base delay for exponential backoff; attempt N waits factor * 2**(N-1).
DEFAULT_BACKOFF_FACTOR = 0.5

# Ceiling on an honoured Retry-After. Without it the appliance could park a
# worker for as long as it likes.
MAX_RETRY_AFTER = 30.0

# Retried because both mean "I did not process this". Nothing else is: a 4xx
# is a bug in the request and will fail identically on every attempt, and a
# 500 may well have applied a partial change.
RETRY_STATUS_CODES = frozenset({429, 503})

# TrueNAS reports "no such object" two different ways, verified against
# TrueNAS-25.10.5 in #11:
#
#   GET    /pool/dataset/id/<missing>  -> 404, body {"message": ""}
#   DELETE /pool/dataset/id/<missing>  -> 422, errno 2
#   PUT    /pool/dataset/id/<missing>  -> 422, errno 2
#   DELETE /iscsi/extent/id/<missing>  -> 422, errno 2
#
# DELETE -- the one operation idempotent deletes actually depend on -- is in
# the second group, so a 404-only mapping would never fire where it matters.
#
# The 422 body is {<field>: [{"message": str, "errno": int}, ...], ...} where
# the field key varies ("null", "id", "pool_dataset_create.name"), so the
# whole body is scanned. `errno`, not the message text, is the discriminator:
# creating into a nonexistent pool returns errno 22 with the message "zpool
# (X) does not exist.", which string matching would misread as "already
# deleted" and report a failed create as a successful delete.
ENOENT = 2


class TrueNASAPIError(Exception):
    """Base class for every error raised by :class:`TrueNASAPIClient`."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        method: Optional[str] = None,
        endpoint: Optional[str] = None,
    ):
        """
        Initialize the error.

        Args:
            message: Human-readable description, safe to log
            status_code: HTTP status code, when the failure had one
            method: HTTP method of the failed request
            endpoint: API path of the failed request, without the prefix
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.method = method
        self.endpoint = endpoint


class TrueNASAPINotFoundError(TrueNASAPIError):
    """The requested object does not exist (HTTP 404).

    The driver catches this to make ``delete_volume``, ``delete_snapshot``
    and ``terminate_connection`` idempotent: an object that is already gone
    is a successful delete, not a failure.

    **Hazard.** A mistyped endpoint path also returns 404 (verified: an
    unrouted path answers ``404: Not Found`` as plain text), so a caller
    that swallows this to gain idempotency will read a wrong path as a
    successful delete. The two are distinguishable by body -- a missing
    object returns JSON, a bad route returns text -- but that is a thin
    reed to lean on. The real guard is exercising every path against real
    hardware with ``tools/verify_endpoints.py`` before relying on it.
    """


class TrueNASAPIAuthError(TrueNASAPIError):
    """Authentication or authorisation failed (HTTP 401 or 403).

    Almost always a bad, revoked, or insufficiently privileged API key.
    Distinct from the base class so this produces an actionable message
    rather than a generic HTTP error.
    """


class TrueNASAPIConnectionError(TrueNASAPIError):
    """The request never completed at the network level.

    Whether the appliance processed it is *unknown*, so callers must not
    assume the operation did not happen.
    """


class TrueNASAPITimeoutError(TrueNASAPIConnectionError):
    """The request exceeded its connect or read timeout.

    A subclass of the connection error because the same caution applies: a
    read timeout means the appliance stopped answering, not that it stopped
    working on the request.
    """


class TrueNASAPIClient:
    """Client for TrueNAS Scale REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: Union[float, Tuple[float, float]] = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ):
        """
        Initialize the TrueNAS client.

        Args:
            base_url: Base URL of the appliance, with or without the
                ``/api/v2.0`` suffix (e.g. ``https://truenas.example.com``)
            api_key: TrueNAS API key for a service account
            verify_ssl: Whether to verify SSL certificates (default True)
            timeout: Per-request timeout in seconds, either a single value
                or a ``(connect, read)`` pair
            max_attempts: Total attempts per request, including the first.
                1 disables retrying.
            backoff_factor: Base delay in seconds for exponential backoff

        Raises:
            ValueError: If max_attempts is less than 1, or if base_url
                carries inline credentials
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        # Reject https://user:pass@host outright. Two independent reasons,
        # both verified against requests 2.x:
        #
        # 1. It does not work. PreparedRequest.prepare_auth() turns the
        #    userinfo into a Basic header that *overwrites* the Bearer key
        #    set below, so the API key is silently discarded and every call
        #    401s.
        # 2. It leaks. requests keeps the userinfo in `response.url`, and
        #    Response.raise_for_status() builds its message as
        #    "<status> <reason> for url: <url>". That HTTPError is chained
        #    as __cause__ of the typed error raised here, so LOG.exception
        #    or any traceback formatting prints the password -- regardless
        #    of how carefully this module words its own messages.
        #
        # Nothing here supports inline credentials (auth is a Bearer API
        # key, #10), so failing loudly beats silently reinterpreting it.
        if urlsplit(base_url).username is not None:
            raise ValueError(
                "base_url must not contain inline credentials "
                "(https://user:pass@host). Authentication uses the "
                "truenas_api_key Bearer token; a userinfo component "
                "overrides it and leaks into logged tracebacks."
            )
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith(API_PREFIX):
            self.base_url = self.base_url[:-len(API_PREFIX)]
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_factor = backoff_factor
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        self.session.verify = verify_ssl

    def _timeout_description(self) -> str:
        """
        Render the configured timeout for a log or error message.

        The default is a ``(connect, read)`` pair, which interpolates as
        "(10.0, 60.0)s" and reads poorly in a log line -- and hides which
        half of the budget was actually exceeded.

        Returns:
            Human-readable description of the timeout budget
        """
        if isinstance(self.timeout, (tuple, list)):
            connect, read = self.timeout
            return f"connect {connect}s, read {read}s"
        return f"{self.timeout}s"

    @staticmethod
    def _response_detail(response: Any, limit: int = 500) -> str:
        """
        Render an error response body for inclusion in a message.

        Never raises. This runs while an exception is being constructed, so
        a failure here would mask the error actually being reported.

        Args:
            response: The response object to render
            limit: Maximum characters to keep

        Returns:
            Whitespace-collapsed body text, truncated, or '' if unreadable
        """
        try:
            text = " ".join(str(response.text).split())
        except Exception:                            # noqa: BLE001
            return ""
        return text[:limit] + "..." if len(text) > limit else text

    @staticmethod
    def _is_enoent(response: Any) -> bool:
        """
        Decide whether a 422 body means "this object does not exist".

        Requires *every* reported error to be ENOENT, not merely one of
        them. The asymmetry is deliberate: a false positive here makes the
        driver treat a genuine failure as a completed delete and drop a
        volume Cinder still believes it removed, whereas a false negative
        only fails a delete that would have been a no-op. Never raises --
        see :meth:`_response_detail`.

        Args:
            response: The 422 response to inspect

        Returns:
            True if the body reports ENOENT and nothing else
        """
        try:
            body = response.json()
        except Exception:                            # noqa: BLE001
            return False
        if not isinstance(body, dict):
            return False
        errnos = [
            entry.get("errno")
            for errors in body.values() if isinstance(errors, list)
            for entry in errors if isinstance(entry, dict)
        ]
        return bool(errnos) and all(errno == ENOENT for errno in errnos)

    def _retry_delay(self, response: Any, attempt: int) -> float:
        """
        Work out how long to wait before retrying a throttled request.

        Honours a numeric ``Retry-After`` header, clamped to
        ``MAX_RETRY_AFTER`` -- clamped rather than discarded, because
        ignoring a server that asked for a long pause is how a rate limit
        gets worse. The HTTP-date form of the header is not parsed; it falls
        back to backoff.

        Args:
            response: The 429/503 response
            attempt: 1-based number of the attempt that just failed

        Returns:
            Delay in seconds
        """
        try:
            raw = response.headers.get("Retry-After")
            retry_after = float(raw) if raw is not None else None
        except (AttributeError, TypeError, ValueError):
            retry_after = None
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, MAX_RETRY_AFTER)
        return self.backoff_factor * (2 ** (attempt - 1))

    def _raise_for_status(
        self,
        method: str,
        endpoint: str,
        response: Any,
    ) -> None:
        """
        Translate an error status into the matching typed exception.

        Args:
            method: HTTP method of the request
            endpoint: API path of the request, without the prefix
            response: The response to check

        Raises:
            TrueNASAPINotFoundError: On HTTP 404, or on a 422 whose body
                reports ENOENT -- see the ``ENOENT`` note above
            TrueNASAPIAuthError: On HTTP 401 or 403
            TrueNASAPIError: On any other error status
        """
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = response.status_code
            detail = self._response_detail(response)
            # `endpoint`, not the full URL. This alone is not a credential
            # guarantee -- the chained HTTPError below carries requests'
            # own "for url: <url>" message, which is outside this module's
            # control. The actual guarantee comes from __init__ refusing a
            # base_url with userinfo; this just keeps the message tidy.
            message = f"TrueNAS API {method} {endpoint} failed: HTTP {status}"
            if detail:
                message = f"{message}: {detail}"
            if status == 404 or (status == 422 and self._is_enoent(response)):
                error = TrueNASAPINotFoundError
            elif status in (401, 403):
                error = TrueNASAPIAuthError
                message = (
                    f"{message}. Check that truenas_api_key is a valid, "
                    f"unrevoked key with sufficient privileges."
                )
            else:
                error = TrueNASAPIError
            raise error(
                message,
                status_code=status,
                method=method,
                endpoint=endpoint,
            ) from exc

    def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> Any:
        """
        Make a request to the TrueNAS API.

        Applies the configured timeout unless the caller passes its own, and
        retries 429/503 with exponential backoff. Nothing else is retried --
        see ``RETRY_STATUS_CODES``. In particular a timeout is *not* retried:
        the appliance may still be processing the request, and replaying a
        create or delete on top of that is worse than failing.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path (without /api/v2.0)
            **kwargs: Additional arguments to pass to requests

        Returns:
            Decoded JSON body, or ``{}`` when the response has no body.
            List endpoints return a list, so callers must not assume a dict.

        Raises:
            TrueNASAPITimeoutError: If the request exceeded its timeout
            TrueNASAPIConnectionError: If the request failed at the network
                level
            TrueNASAPIError: If the appliance returned an error status; see
                :meth:`_raise_for_status` for the subclass mapping
        """
        url = f"{self.base_url}{API_PREFIX}{endpoint}"
        kwargs.setdefault("timeout", self.timeout)

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.Timeout as exc:
                raise TrueNASAPITimeoutError(
                    f"TrueNAS API {method} {endpoint} timed out "
                    f"({self._timeout_description()}). The appliance may "
                    f"still be processing it.",
                    method=method,
                    endpoint=endpoint,
                ) from exc
            except requests.RequestException as exc:
                raise TrueNASAPIConnectionError(
                    f"TrueNAS API {method} {endpoint} could not be "
                    f"completed: {type(exc).__name__}",
                    method=method,
                    endpoint=endpoint,
                ) from exc

            if (
                response.status_code in RETRY_STATUS_CODES
                and attempt < self.max_attempts
            ):
                delay = self._retry_delay(response, attempt)
                LOG.warning(
                    "TrueNAS API %s %s returned HTTP %s; retrying in %.1fs "
                    "(attempt %d of %d).",
                    method, endpoint, response.status_code, delay,
                    attempt, self.max_attempts,
                )
                time.sleep(delay)
                continue

            self._raise_for_status(method, endpoint, response)

            # DELETE and other 204s return an empty body; json() would raise.
            if not response.content:
                return {}

            return response.json()

    def best_effort_delete(
        self,
        delete: Callable[..., Any],
        *args,
        what: str = "resource",
        **kwargs
    ) -> bool:
        """
        Delete something during rollback, logging failures instead of raising.

        Rollback runs inside an ``except`` block that is about to re-raise the
        original error. If cleanup raised too it would replace that error with
        a less useful one, so this swallows and logs -- the one place in this
        module where a bare ``except`` is correct.

        The pipeline sequencing itself lives in the driver (#12); this is only
        the primitive it needs::

            extent = client.create_iscsi_extent(...)
            try:
                target = client.create_iscsi_target(...)
            except TrueNASAPIError:
                client.best_effort_delete(
                    client.delete_iscsi_extent, extent["id"],
                    what=f"iSCSI extent {extent['id']}")
                raise

        Args:
            delete: The delete method to call
            *args: Positional arguments for it
            what: Description of the object, used in log messages
            **kwargs: Keyword arguments for it

        Returns:
            True if the object is gone (deleted, or already absent), False if
            cleanup failed and it may now be orphaned
        """
        try:
            delete(*args, **kwargs)
        except TrueNASAPINotFoundError:
            LOG.debug("Rollback: %s was already gone.", what)
        except Exception:                            # noqa: BLE001
            LOG.exception(
                "Rollback failed to remove %s. It may now be orphaned on the "
                "appliance and need manual cleanup. Continuing so the "
                "original error is not masked.", what,
            )
            return False
        return True

    def is_eula_accepted(self) -> bool:
        """
        Check if the TrueNAS End-User License Agreement (EULA) is accepted.

        The endpoint returns a bare JSON boolean (``true``/``false``), not an
        object -- verified against TrueNAS-25.10.5 in #35.

        Returns:
            True if EULA is accepted, False otherwise
        """
        return bool(self._make_request("GET", "/truenas/is_eula_accepted"))

    def get_pool_list(self) -> List[Dict[str, Any]]:
        """
        Get list of available storage pools.

        Returns:
            List of pool information dictionaries
        """
        return self._make_request("GET", "/pool")

    @staticmethod
    def _dataset_id(pool: str, name: str) -> str:
        """
        Build the URL-encoded dataset identifier for a zvol.

        TrueNAS addresses datasets by their full ZFS path, with the separator
        percent-encoded: ``tank/vol1`` becomes ``tank%2Fvol1``. Nested names
        encode every separator, so ``tank`` + ``proxmox/vm-100`` becomes
        ``tank%2Fproxmox%2Fvm-100``.

        Args:
            pool: Pool name
            name: Zvol name, which may itself contain '/'

        Returns:
            Percent-encoded dataset identifier
        """
        return quote(f"{pool}/{name}", safe="")

    def create_zvol(
        self,
        pool: str,
        name: str,
        size_gb: int,
        sparse: bool = True,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new zvol (block device).

        Args:
            pool: Name of the pool to create the zvol in
            name: Name for the zvol
            size_gb: Size of the zvol in GB
            sparse: Whether to thin-provision the zvol (default True)
            **kwargs: Additional dataset properties

        Returns:
            Information about the created zvol
        """
        # No `volmode`: it is FreeBSD terminology and TrueNAS Scale rejects
        # it. Worse, an unrecognised key breaks discrimination of the VOLUME
        # schema variant, so the request falls through to the FILESYSTEM
        # schema and every volume-only field is reported as unexpected --
        # a 422 that points at `type` rather than the real culprit. See #35.
        payload = {
            "name": f"{pool}/{name}",
            "type": "VOLUME",
            "volsize": size_gb * GIB,
            "sparse": sparse,
            **kwargs
        }
        return self._make_request("POST", "/pool/dataset", json=payload)

    def get_zvol(self, pool: str, name: str) -> Dict[str, Any]:
        """
        Get a single zvol by pool and name.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name

        Returns:
            Zvol metadata, including volsize
        """
        dataset_id = self._dataset_id(pool, name)
        return self._make_request("GET", f"/pool/dataset/id/{dataset_id}")

    def delete_zvol(
        self,
        pool: str,
        name: str,
        recursive: bool = False
    ) -> None:
        """
        Delete a zvol.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name
            recursive: Whether to delete dependent children (default False)
        """
        dataset_id = self._dataset_id(pool, name)
        self._make_request(
            "DELETE",
            f"/pool/dataset/id/{dataset_id}",
            json={"recursive": recursive},
        )

    def resize_zvol(
        self,
        pool: str,
        name: str,
        new_size_gb: int
    ) -> Dict[str, Any]:
        """
        Resize an existing zvol.

        ZFS supports online growth, so no iSCSI reconnect is required. This
        does not shrink a zvol -- ZFS rejects a volsize below current usage.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name
            new_size_gb: New size in GB

        Returns:
            Updated zvol metadata
        """
        dataset_id = self._dataset_id(pool, name)
        return self._make_request(
            "PUT",
            f"/pool/dataset/id/{dataset_id}",
            json={"volsize": new_size_gb * GIB},
        )

    def list_zvols(self, pool: str) -> List[Dict[str, Any]]:
        """
        List every zvol in a pool.

        Args:
            pool: Pool to list zvols from

        Returns:
            List of zvol metadata dictionaries
        """
        # `name__^` is TrueNAS's startswith operator. `name__startswith` is
        # rejected with "Invalid operation: startswith", and the JSON
        # `filters=[[...]]` form is worse -- it returns 200 with an empty
        # list, so a wrong query reads as "no volumes exist". See #35.
        return self._make_request(
            "GET",
            "/pool/dataset",
            params={"type": "VOLUME", "name__^": f"{pool}/"},
        )

    def get_iscsi_target_list(self) -> List[Dict[str, Any]]:
        """
        Get list of iSCSI targets.

        Returns:
            List of iSCSI target information dictionaries
        """
        return self._make_request("GET", "/iscsi/target")

    def create_iscsi_target(
        self,
        name: str,
        alias: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new iSCSI target.

        Args:
            name: Name for the iSCSI target
            alias: Optional alias for the target
            **kwargs: Additional target properties

        Returns:
            Information about the created target
        """
        payload = {
            "name": name,
            "alias": alias,
            **kwargs
        }
        return self._make_request("POST", "/iscsi/target", json=payload)

    def delete_iscsi_target(self, id: int) -> None:
        """
        Delete an iSCSI target by ID.

        Args:
            id: Target ID to delete
        """
        self._make_request("DELETE", f"/iscsi/target/id/{id}")

    def create_iscsi_extent(
        self,
        name: str,
        path: str,
        disk_type: str = "Disk",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new iSCSI extent (maps a zvol to an iSCSI target).

        Args:
            name: Name for the extent
            path: Path to the zvol (e.g., "/dev/zvol/tank/volume1")
            disk_type: Type of disk (default "Disk")
            **kwargs: Additional extent properties

        Returns:
            Information about the created extent
        """
        payload = {
            "name": name,
            "type": disk_type,
            "path": path,
            **kwargs
        }
        return self._make_request("POST", "/iscsi/extent", json=payload)

    def delete_iscsi_extent(self, id: int) -> None:
        """
        Delete an iSCSI extent by ID.

        Args:
            id: Extent ID to delete
        """
        self._make_request("DELETE", f"/iscsi/extent/id/{id}")

    def create_iscsi_target_extent(
        self,
        target_id: int,
        extent_id: int,
        lun_id: int = 0
    ) -> Dict[str, Any]:
        """
        Associate an iSCSI extent with a target.

        Args:
            target_id: ID of the iSCSI target
            extent_id: ID of the iSCSI extent
            lun_id: LUN number (default 0)

        Returns:
            Information about the created association
        """
        payload = {
            "target": target_id,
            "extent": extent_id,
            "lunid": lun_id
        }
        return self._make_request("POST", "/iscsi/targetextent", json=payload)

    def delete_iscsi_target_extent(self, id: int) -> None:
        """
        Delete an iSCSI target-extent association by ID.

        Args:
            id: Target-extent association ID to delete
        """
        self._make_request("DELETE", f"/iscsi/targetextent/id/{id}")

    def get_snapshot_list(self) -> List[Dict[str, Any]]:
        """
        Get list of snapshots.

        Returns:
            List of snapshot information dictionaries
        """
        return self._make_request("GET", "/zfs/snapshot")

    def create_snapshot(
        self,
        dataset: str,
        name: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new snapshot.

        Args:
            dataset: Dataset name (e.g., "tank/volume1")
            name: Snapshot name
            **kwargs: Additional snapshot options

        Returns:
            Information about the created snapshot
        """
        payload = {
            "dataset": dataset,
            "name": name,
            **kwargs
        }
        return self._make_request("POST", "/zfs/snapshot", json=payload)

    def delete_snapshot(self, id: str) -> None:
        """
        Delete a snapshot by ID.

        Args:
            id: Snapshot ID to delete
        """
        self._make_request("DELETE", f"/zfs/snapshot/id/{id}")
