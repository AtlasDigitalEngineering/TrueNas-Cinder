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

# Payload keys `create_zvol` sets itself that a caller could still reach:
# `**kwargs` is spread after them, so either would win silently (#36).
#
# Only these two. `name` is a named parameter, so Python rejects a second
# value for it before the body runs, and `volmode` left the payload in
# #35. `create_snapshot` needs no equivalent at all -- both of its
# structural keys, `dataset` and `name`, are named parameters, so nothing
# structural can reach its `**kwargs`. Verified rather than assumed; see
# `test_create_snapshot_structural_keys_cannot_reach_kwargs`.
ZVOL_RESERVED_KEYS: Tuple[str, ...] = ("type", "volsize")

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
# The 422 body comes in two shapes, and both have to be understood:
#
#   nested  {<field>: [{"message": str, "errno": int}, ...], ...}
#   flat    {"message": str, "errno": int}
#
# Everything above uses the nested form, where the field key varies ("null",
# "id", "pool_dataset_create.name"), so the whole body is scanned. The
# /pool/dataset/rename and /pool/snapshot/rename endpoints added in #20 use
# the flat form instead; before it was handled, a rename of a missing source
# raised a plain TrueNASAPIError and the ENOENT mapping never fired.
#
# `errno`, not the message text, is the discriminator: creating into a
# nonexistent pool returns errno 22 with the message "zpool (X) does not
# exist.", which string matching would misread as "already deleted" and
# report a failed create as a successful delete. Note that a missing
# *snapshot* rename source reports errno 14, not 2, so it is deliberately
# not an ENOENT -- see rename_snapshot.
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
    hardware with the functional suite before relying on it.
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


def _reject_reserved(method: str, reserved: Tuple[str, ...],
                     kwargs: Dict[str, Any]) -> None:
    """Refuse a pass-through key that would rewrite the request's shape.

    The create methods spread `**kwargs` into the payload *after* the
    fields they set themselves, so a caller passing `type=` or `volsize=`
    silently replaces one. The mistake is invisible where it is made and
    loud somewhere unrelated: a FILESYSTEM created where a VOLUME was
    meant produces no `/dev/zvol` node, and the failure surfaces later as
    an iSCSI extent error pointing at the extent (#36).

    Raising says so at the call site instead. A caller passing one of
    these has misunderstood the method, which is worth reporting rather
    than quietly honouring or quietly ignoring.

    Args:
        method: Name of the calling method, for the message
        reserved: Keys that method sets itself
        kwargs: The caller's pass-through arguments

    Raises:
        ValueError: If any reserved key is present
    """
    clashing = sorted(set(kwargs) & set(reserved))
    if clashing:
        raise ValueError(
            "%s sets %s itself; passing %s through **kwargs would "
            "silently replace it. Remove %s from the call."
            % (method, ", ".join(reserved), ", ".join(clashing),
               " and ".join(clashing)))


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

        Handles both body shapes described above. The flat form carries a
        single error, so "every reported error" and "the reported error"
        coincide there.

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
        # Membership, not an isinstance check: a non-integer errno simply
        # fails the ENOENT comparison below, so narrowing the type here
        # would only add a branch that can never change the outcome.
        if not errnos and "errno" in body:
            errnos = [body["errno"]]
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
            TrueNASAPIAuthError: On HTTP 401 or 403, with different
                messages. 401 means the key itself was rejected; 403 means
                it was accepted and the account lacks a role. The remedies
                are opposite, so a shared message sends half of all
                readers to fix the wrong thing.
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
                # 401 and 403 need opposite actions, and reporting them
                # identically sends an operator to reissue a key that was
                # never the problem. Same exception class either way, so
                # `except TrueNASAPIAuthError` still catches both.
                error = TrueNASAPIAuthError
                if status == 403:
                    # Names FULL_ADMIN rather than pointing at the docs.
                    # This message is what most 403s reach an operator as:
                    # only check_for_setup_error special-cases auth errors,
                    # so anything failing mid-operation surfaces this text
                    # verbatim -- possibly inside a container where no repo
                    # path can be opened. "the roles listed in the docs"
                    # also reads as the eleven-role table, which is
                    # documented as *not* working today.
                    message = (
                        f"{message}. The key was accepted, so it is valid -- "
                        f"the account it belongs to lacks the role this "
                        f"call needs. Grant that account FULL_ADMIN; do not "
                        f"reissue the key."
                    )
                else:
                    message = (
                        f"{message}. The key was rejected outright: it is "
                        f"wrong, revoked or expired. Issue a new one and set "
                        f"truenas_api_key. This is not a role problem."
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

        The pipeline sequencing itself lives in the driver; this is only the
        primitive it needs::

            extent_id = client.create_extent(zvol_path, name)
            try:
                target_id = client.create_target(name, group_id, portal_ids)
            except TrueNASAPIError:
                client.best_effort_delete(
                    client.delete_extent, extent_id,
                    what=f"iSCSI extent {extent_id}")
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
        _reject_reserved("create_zvol", ZVOL_RESERVED_KEYS, kwargs)
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

    def rename_zvol(
        self,
        pool: str,
        name: str,
        new_name: str
    ) -> None:
        """
        Rename a zvol within its pool.

        A ZFS rename is metadata-only: no data moves and the dataset keeps
        its identity, verified in #20 by `creation` surviving the rename
        byte-for-byte while the source id disappeared. That is what makes
        adopting a Proxmox-created zvol free rather than a full copy.

        Two traps, both confirmed against TrueNAS-25.10.5:

        `new_name` is a **full dataset path**, not a leaf. The leaf form is
        rejected with "cannot create 'volume-x': missing dataset name".
        This method takes the name relative to the pool, matching
        :meth:`create_zvol`, and builds the path itself.

        `force` is **mandatory** -- the appliance refuses without it even
        when nothing is using the dataset. It therefore performs no safety
        check on the caller's behalf, and the caller must establish that
        the zvol is not already exported before calling this.

        Args:
            pool: Pool the zvol lives in
            name: Current zvol name relative to the pool; may contain '/'
            new_name: New zvol name relative to the pool

        Raises:
            TrueNASAPINotFoundError: If the source zvol does not exist;
                the appliance reports this as 422 with errno 2
            TrueNASAPIError: If the rename was refused for any other
                reason. A destination that already exists is errno 14, not
                errno 2, so it stays a plain error rather than being
                mistaken for a missing source.
        """
        self._make_request(
            "POST",
            "/pool/dataset/rename",
            json={
                "id": f"{pool}/{name}",
                "data": {
                    "new_name": f"{pool}/{new_name}",
                    "force": True,
                },
            },
        )

    def clone_snapshot(
        self,
        snapshot_id: str,
        pool: str,
        dest_name: str,
        **properties
    ) -> None:
        """
        Clone a snapshot into a new zvol.

        A ZFS clone is a writable dataset sharing the snapshot's blocks, so
        it is created instantly and consumes only what it later diverges
        by. The cost is a permanent dependency: the clone's ``origin``
        points at the snapshot, and **that snapshot cannot be destroyed
        while the clone exists**, which also blocks destroying the dataset
        the snapshot sits on.

        :meth:`promote_clone` moves that dependency rather than removing
        it. See its docstring before deciding whether to call it -- the
        choice is not obvious and it is not free.

        Args:
            snapshot_id: Snapshot to clone from, ``pool/dataset@snapshot``
            pool: Pool to create the clone in
            dest_name: Name for the new zvol, relative to the pool
            **properties: ZFS properties to set on the clone, passed as
                ``dataset_properties``

        Raises:
            TrueNASAPIError: If the snapshot does not exist, or the
                destination name is taken
        """
        payload = {
            "snapshot": snapshot_id,
            "dataset_dst": f"{pool}/{dest_name}",
        }
        if properties:
            payload["dataset_properties"] = properties
        self._make_request("POST", "/pool/snapshot/clone", json=payload)

    def promote_clone(self, pool: str, name: str) -> None:
        """
        Promote a clone so it no longer depends on its origin snapshot.

        **This reverses the dependency; it does not remove it**, and it
        moves the snapshot. Verified against TrueNAS-25.10.5 in #13:

        ===================  ======================  ======================
        ..                   before promote          after promote
        ===================  ======================  ======================
        snapshots on source  ``[src@s1]``            ``[]``
        snapshots on clone   ``[]``                  ``[clone@s1]``
        ``origin`` of source  --                     ``clone@s1``
        ``origin`` of clone  ``src@s1``              --
        ===================  ======================  ======================

        Afterwards the source can be destroyed, which is the reason to do
        it. But the snapshot now lives on the clone, so anything that
        resolves that snapshot by its old dataset will no longer find it,
        and the clone cannot be destroyed while the source survives.

        Callers must therefore know whether a Cinder snapshot record refers
        to the snapshot being moved. #21 owns that decision.

        Note the request carries **no body at all**. An empty JSON object
        is rejected -- the middleware counts it as a second positional
        argument alongside the id: ``Too many arguments (expected 1, found
        2)``.

        Args:
            pool: Pool the clone lives in
            name: Clone's zvol name, relative to the pool

        Raises:
            TrueNASAPINotFoundError: If no such dataset exists
            TrueNASAPIError: If the dataset is not a clone, or the promote
                was refused
        """
        dataset_id = self._dataset_id(pool, name)
        self._make_request("POST", f"/pool/dataset/id/{dataset_id}/promote")

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

    # ------------------------------------------------------------------
    # iSCSI pipeline
    #
    # Attaching one volume wires five resources together:
    #
    #   portal        the IP:port the target listens on (appliance-wide)
    #   initiator     the group of IQNs permitted to connect
    #   extent        the zvol presented as a logical unit
    #   target        the thing an initiator logs in to
    #   targetextent  joins a target to an extent at a LUN
    #
    # Every payload below was verified against TrueNAS-25.10.5 (#12), and
    # the appliance's own OpenAPI document is the source for the field
    # names and enums. Two of the design spec's assumptions were wrong --
    # see `create_extent` and the delete methods.
    # ------------------------------------------------------------------

    @staticmethod
    def _created_id(response: Any, what: str) -> int:
        """
        Pull the new resource's ID out of a create response.

        Args:
            response: Decoded body of the create request
            what: Description of the resource, for the error message

        Returns:
            The new resource's numeric ID

        Raises:
            TrueNASAPIError: If the response carried no usable ID, which
                means the appliance's contract changed under us
        """
        if isinstance(response, dict) and isinstance(
            response.get("id"), int
        ):
            return response["id"]
        raise TrueNASAPIError(
            f"TrueNAS accepted the {what} but returned no usable id: "
            f"{TrueNASAPIClient._truncate(response)}"
        )

    @staticmethod
    def _truncate(value: Any, limit: int = 200) -> str:
        """
        Render a value for an error message, bounded in length.

        Args:
            value: Value to render
            limit: Maximum characters to keep

        Returns:
            Truncated string form of the value
        """
        text = str(value)
        return text[:limit] + "..." if len(text) > limit else text

    def _get_one_by_name(
        self,
        resource: str,
        name: str,
        what: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch exactly one iSCSI resource by name, or None.

        Filtering happens on the appliance. Verified in #16 to discriminate
        correctly on both ``/iscsi/target`` and ``/iscsi/extent``: with two
        exports present, an exact name returns the single matching row and
        a name matching nothing returns ``[]``.

        **More than one row is an error, not a result.** An unrecognised
        filter *field* is not rejected -- TrueNAS answers 200 with an empty
        list rather than complaining, and a filter it ignored would return
        the whole collection. Taking ``[0]`` from an unfiltered collection
        would hand the caller some other volume's export and delete it, so
        this refuses to guess. (An invalid *operator* does 422 loudly; it
        is the field name that fails quietly.)

        Args:
            resource: Resource path segment, e.g. ``"target"``
            name: Exact name to match
            what: Human-readable description, for the error message

        Returns:
            The matching resource, or None if nothing has that name

        Raises:
            TrueNASAPIError: If the appliance returns more than one match
        """
        rows = self._make_request(
            "GET", f"/iscsi/{resource}", params={"name": name},
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise TrueNASAPIError(
                f"Expected at most one {what} named {name!r} but got "
                f"{len(rows)}. The name filter appears to have been "
                f"ignored; refusing to guess which one is correct.",
                method="GET",
                endpoint=f"/iscsi/{resource}",
            )
        return rows[0]

    @staticmethod
    def zvol_disk_path(pool: str, name: str) -> str:
        """
        Build the value the extent ``disk`` field expects for a zvol.

        ``zvol/{pool}/{name}`` -- note the absence of a leading ``/dev/``.
        Verified against ``GET /iscsi/extent/disk_choices``, which is the
        appliance's own list of acceptable values and returned exactly
        ``zvol/Dev-Pool/<name>`` for a freshly created zvol (#12).

        Args:
            pool: Pool the zvol lives in
            name: Zvol name

        Returns:
            Value for the extent's ``disk`` field
        """
        return f"zvol/{pool}/{name}"

    def get_iscsi_global_config(self) -> Dict[str, Any]:
        """
        Get the appliance-wide iSCSI configuration.

        The useful field is ``basename`` -- the IQN prefix every target
        name hangs off, e.g. ``iqn.2005-10.org.freenas.ctl``. A target's
        full IQN is ``{basename}:{target_name}``, which is what
        ``initialize_connection`` must hand Nova. Reading it here is what
        lets #17 stop hardcoding the prefix.

        Returns:
            Global config, including ``basename`` and ``listen_port``
        """
        return self._make_request("GET", "/iscsi/global")

    def get_portals(self) -> List[Dict[str, Any]]:
        """
        List the configured iSCSI portals.

        **A fresh appliance has none.** The design spec described portals
        as pre-existing and read-only from the driver's perspective; on a
        clean TrueNAS install this returns ``[]`` (#12). Callers must
        handle that rather than assuming a portal is available.

        Returns:
            List of portals, each with ``id``, ``listen`` and ``tag``
        """
        return self._make_request("GET", "/iscsi/portal")

    def create_portal(
        self,
        listen_ips: Optional[List[str]] = None,
        comment: str = "",
    ) -> int:
        """
        Create an iSCSI portal.

        Only the addresses offered by ``/iscsi/portal/listen_ip_choices``
        are accepted; on a single-NIC appliance that is ``0.0.0.0`` and
        ``::`` only. The port is not settable per portal -- it comes from
        ``listen_port`` in the global config.

        Whether the driver should create a portal on demand or require one
        to pre-exist is a policy decision left to #14/#17. This is only the
        primitive.

        Args:
            listen_ips: Addresses to listen on (default ``["0.0.0.0"]``)
            comment: Optional description stored on the portal

        Returns:
            ID of the new portal
        """
        payload = {
            "listen": [{"ip": ip} for ip in (listen_ips or ["0.0.0.0"])],
            "comment": comment,
        }
        response = self._make_request("POST", "/iscsi/portal", json=payload)
        return self._created_id(response, "iSCSI portal")

    def get_initiator_groups(self) -> List[Dict[str, Any]]:
        """
        List the configured initiator groups.

        Returns:
            List of groups, each with ``id`` and ``initiators``
        """
        return self._make_request("GET", "/iscsi/initiator")

    def get_or_create_initiator_group(
        self,
        initiator_iqns: List[str],
    ) -> int:
        """
        Find an initiator group holding exactly these IQNs, or create one.

        **TrueNAS enforces no uniqueness here.** Posting the same
        ``initiators`` list twice yields two separate groups (verified in
        #12), so the deduplication has to happen on this side or every
        attach leaks a new group. Matching is on set equality, since the
        order TrueNAS stores the list in is not guaranteed to be the order
        it was sent in.

        This is a read-modify-write and races under concurrent attach --
        two workers can both miss and both create. #18 owns the locking.

        Args:
            initiator_iqns: IQNs permitted to connect. Must not be empty.

        Returns:
            ID of the matching or newly created group

        Raises:
            ValueError: If ``initiator_iqns`` is empty. TrueNAS reads an
                empty list as "allow every initiator", so an accidental
                empty list would silently expose the volume to the whole
                network rather than failing.
        """
        if not initiator_iqns:
            raise ValueError(
                "initiator_iqns must not be empty: TrueNAS treats an empty "
                "initiators list as 'allow all initiators', which would "
                "expose the volume to every host that can reach the portal."
            )
        wanted = set(initiator_iqns)
        for group in self.get_initiator_groups():
            if set(group.get("initiators") or []) == wanted:
                LOG.debug(
                    "Reusing iSCSI initiator group %s for %s.",
                    group.get("id"), sorted(wanted),
                )
                return group["id"]
        response = self._make_request(
            "POST", "/iscsi/initiator", json={"initiators": initiator_iqns},
        )
        return self._created_id(response, "iSCSI initiator group")

    def get_extents(self) -> List[Dict[str, Any]]:
        """
        List the configured iSCSI extents.

        Returns:
            List of extents, each with ``id``, ``name`` and ``disk``
        """
        return self._make_request("GET", "/iscsi/extent")

    def get_extent_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find the iSCSI extent with this exact name.

        The extent is the half of the pipeline that survives a target
        delete -- deleting a target cascades only the target-extent link --
        so teardown has to find and remove it separately. See
        :meth:`get_target_by_name` for why the lookup is by name.

        Args:
            name: Extent name

        Returns:
            The extent, or None if no extent has that name
        """
        return self._get_one_by_name("extent", name, "iSCSI extent")

    def create_extent(self, zvol_path: str, extent_name: str) -> int:
        """
        Present a zvol as an iSCSI extent.

        ``type`` and ``blocksize`` are deliberately not sent: the
        appliance defaults them to ``DISK`` and ``512``, which is what we
        want, and every field omitted is one that cannot drift.

        The shipped payload this replaces was wrong twice over, both
        confirmed by sending it (#12): ``type: "Disk"`` is rejected
        because the enum is ``['DISK', 'FILE']``, and passing the zvol as
        ``path`` fails with "iscsi_extent_create.disk: This field is
        required" -- ``path`` is the *file*-extent field. The response
        echoes the value back in both ``disk`` and ``path``, which is
        probably where the original mistake came from.

        A zvol can back at most one extent; a second attempt fails with
        "Disk currently in use by extent <name>" (errno 22, *not* ENOENT,
        so it surfaces as a plain :class:`TrueNASAPIError`).

        Args:
            zvol_path: Value for ``disk``, as built by
                :meth:`zvol_disk_path` -- ``zvol/{pool}/{name}``
            extent_name: Name for the extent

        Returns:
            ID of the new extent
        """
        payload = {"name": extent_name, "disk": zvol_path}
        response = self._make_request("POST", "/iscsi/extent", json=payload)
        return self._created_id(response, "iSCSI extent")

    def delete_extent(self, extent_id: int) -> None:
        """
        Delete an iSCSI extent.

        Sends no options, which matters. ``remove`` would delete the
        backing file of a file-based extent and ``force`` would delete an
        extent that is in use; both default to false and the driver must
        leave them there.

        Deleting an extent **cascades to its target-extent link** (#12) --
        contrary to the design spec's claim that TrueNAS does not cascade.
        The target survives.

        Args:
            extent_id: Extent ID to delete
        """
        self._make_request("DELETE", f"/iscsi/extent/id/{extent_id}")

    def get_targets(self) -> List[Dict[str, Any]]:
        """
        List the configured iSCSI targets.

        Returns:
            List of targets, each with ``id``, ``name`` and ``groups``
        """
        return self._make_request("GET", "/iscsi/target")

    def get_target_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find the iSCSI target with this exact name.

        The authoritative lookup for teardown (#16). Target names are a
        deterministic function of the Cinder volume name, so the mapping
        survives a ``cinder-volume`` restart and out-of-band changes on the
        appliance without anything having been persisted. Nothing cached is
        trusted for deletion: TrueNAS ids are small integers and a stale one
        could address a different volume's export.

        Args:
            name: Target name, without the IQN basename prefix

        Returns:
            The target, or None if no target has that name
        """
        return self._get_one_by_name("target", name, "iSCSI target")

    def validate_target_name(self, name: str) -> Optional[str]:
        """
        Ask the appliance whether a target name is acceptable.

        TrueNAS allows only lowercase alphanumerics plus ``.``, ``-`` and
        ``:``. Cinder's default ``volume-<uuid>`` passes, but a deployment
        that sets ``volume_name_template`` with an underscore or any
        uppercase letter does not -- and would otherwise only discover
        that at first attach. #14 should call this during ``do_setup``.

        Args:
            name: Candidate target name

        Returns:
            None if the name is acceptable, otherwise the appliance's
            explanation of why it is not
        """
        return self._make_request(
            "POST", "/iscsi/target/validate_name", json={"name": name},
        )

    def create_target(
        self,
        target_name: str,
        initiator_group_id: int,
        portal_ids: Union[int, List[int]],
    ) -> int:
        """
        Create an iSCSI target bound to one or more portals.

        A target carries one group per portal, all sharing the same
        initiator group -- which IQNs may connect is a property of the
        volume, not of the path they arrive by. Binding several portals is
        how multipath is presented: one target, one LUN, several routes to
        it. Verified against TrueNAS-25.10.5 in #45.

        ``authmethod`` is left at ``NONE``. CHAP is deferred past v1 (#27),
        and the driver must never invent a secret -- see #15.

        **The appliance does not preserve group order.** Posting portals
        ``[11, 12]`` returns them as ``[12, 11]`` (#45). Nothing may derive
        an ordering from the response: in ``provider_location`` the first
        portal becomes the singular ``target_portal`` that a non-multipath
        connector uses, and reading it back would let that flip between
        attaches. This method returns only the new id precisely so there is
        no reordered ``groups`` list to be tempted by -- build the order
        from the configured address list instead.

        Args:
            target_name: Name for the target. Becomes the suffix of the
                full IQN, ``{basename}:{target_name}``.
            initiator_group_id: Group of IQNs permitted to connect
            portal_ids: One portal id, or a list of them for multipath.
                Order is not meaningful to the appliance. Each must be an
                integer -- `bool` is refused, see below.

        Returns:
            ID of the new target

        Raises:
            ValueError: If no portal is given, one is repeated, or any is
                not an integer. All are caller bugs: a target with no portal
                is unreachable, a duplicate would ask for two identical
                groups, and a `bool` -- an `int` subclass -- would serialise
                as JSON `true` rather than a portal id.
        """
        # `bool` needs no mention here: it is an `int` subclass, so a bare
        # True already normalises to [True] and is caught by the per-id
        # check below.
        if isinstance(portal_ids, int) or not hasattr(
            portal_ids, "__iter__"
        ):
            portal_ids = [portal_ids]
        else:
            portal_ids = list(portal_ids)
        for portal_id in portal_ids:
            # `bool` is an `int` subclass, so a stray True would otherwise
            # pass an isinstance check and serialise as JSON `true` for the
            # portal id. Rejected explicitly rather than left to fail at
            # the appliance as a schema error (#51).
            if not isinstance(portal_id, int) or isinstance(portal_id, bool):
                raise ValueError(
                    f"create_target got {portal_id!r} as a portal id. "
                    f"Portal ids are integers, as returned by "
                    f"create_portal() and get_portals()."
                )
        if not portal_ids:
            raise ValueError(
                "create_target requires at least one portal: a target with "
                "no portal group is not reachable by any initiator."
            )
        if len(set(portal_ids)) != len(portal_ids):
            raise ValueError(
                f"create_target got a repeated portal id in {portal_ids}. "
                f"Each portal may appear at most once; a duplicate would "
                f"request two identical groups on the same target."
            )
        payload = {
            "name": target_name,
            "groups": self.target_groups(initiator_group_id, portal_ids),
        }
        response = self._make_request("POST", "/iscsi/target", json=payload)
        return self._created_id(response, "iSCSI target")

    @staticmethod
    def target_groups(initiator_group_id, portal_ids):
        """Build a target's ``groups`` payload.

        Shared by :meth:`create_target` and :meth:`update_target_groups` so
        the two cannot drift -- a target updated with a different shape than
        it was created with would be a subtle and hard-to-spot bug.

        Args:
            initiator_group_id: Group of IQNs permitted to connect
            portal_ids: One portal id, or a list of them

        Returns:
            List of group dicts for the ``groups`` field
        """
        if isinstance(portal_ids, int):
            portal_ids = [portal_ids]
        return [
            {
                "portal": portal_id,
                "initiator": initiator_group_id,
                "authmethod": "NONE",
            }
            for portal_id in portal_ids
        ]

    def update_target_groups(self, target_id, initiator_group_id,
                             portal_ids):
        """Repoint an existing target at an initiator group and portals.

        Needed when adopting a target left behind by an earlier attach:
        it carries the *previous* initiator group, so a volume re-attaching
        on a different compute host would be denied by an access list that
        still names the old one (#62).

        Sends only ``groups``; every other field on the target is left as
        it is.

        Args:
            target_id: Target to update
            initiator_group_id: Group of IQNs permitted to connect
            portal_ids: One portal id, or a list of them

        Returns:
            The updated target
        """
        return self._make_request(
            "PUT", f"/iscsi/target/id/{target_id}",
            json={"groups": self.target_groups(initiator_group_id,
                                               portal_ids)},
        )

    def delete_target(self, target_id: int) -> None:
        """
        Delete an iSCSI target.

        Sends no options. ``delete_extents`` would widen this into
        deleting the backing extents too, which for a Cinder volume means
        destroying the export of a volume that still exists. ``force``
        would delete a target with a live session. Both default to false
        and must stay there.

        Deleting a target **cascades to its target-extent links** (#12).
        The extent survives.

        Args:
            target_id: Target ID to delete
        """
        self._make_request("DELETE", f"/iscsi/target/id/{target_id}")

    def get_target_extents(self) -> List[Dict[str, Any]]:
        """
        List the configured target-to-extent associations.

        Returns:
            List of links, each with ``id``, ``target``, ``extent`` and
            ``lunid``
        """
        return self._make_request("GET", "/iscsi/targetextent")

    def get_target_extent(
        self,
        target_id: int,
        extent_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Find the association between a specific target and extent.

        Filtered on both fields at the appliance. As with
        :meth:`_get_one_by_name`, more than one match means the filter was
        not applied and the whole collection came back, so it is refused
        rather than indexed into.

        Args:
            target_id: Target id
            extent_id: Extent id

        Returns:
            The association, or None if the two are not linked

        Raises:
            TrueNASAPIError: If the appliance returns more than one match
        """
        rows = self._make_request(
            "GET", "/iscsi/targetextent",
            params={"target": target_id, "extent": extent_id},
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise TrueNASAPIError(
                f"Expected at most one association between iSCSI target "
                f"{target_id} and extent {extent_id} but got {len(rows)}. "
                f"The filter appears to have been ignored; refusing to "
                f"guess which one is correct.",
                method="GET",
                endpoint="/iscsi/targetextent",
            )
        return rows[0]

    def create_target_extent(
        self,
        target_id: int,
        extent_id: int,
        lun_id: int = 0,
    ) -> int:
        """
        Associate an extent with a target at a LUN.

        Args:
            target_id: ID of the target
            extent_id: ID of the extent
            lun_id: LUN number (default 0)

        Returns:
            ID of the new association
        """
        payload = {
            "target": target_id,
            "extent": extent_id,
            "lunid": lun_id,
        }
        response = self._make_request(
            "POST", "/iscsi/targetextent", json=payload,
        )
        return self._created_id(response, "iSCSI target-extent link")

    def delete_target_extent(self, targetextent_id: int) -> None:
        """
        Delete a target-to-extent association.

        Safe to call after deleting either end: the link is cascaded from
        both sides, and the resulting "does not exist" comes back as 422
        with errno 2, which maps to :class:`TrueNASAPINotFoundError` and
        is swallowed by :meth:`best_effort_delete`.

        Args:
            targetextent_id: Association ID to delete
        """
        self._make_request(
            "DELETE", f"/iscsi/targetextent/id/{targetextent_id}",
        )

    def get_iscsi_sessions(self) -> List[Dict[str, Any]]:
        """
        List the iSCSI sessions currently connected to the appliance.

        This is the only way to tell "a zvol has an export configured"
        apart from "a zvol is being served right now", and adoption (#20)
        turns on that distinction: renaming a zvol out from under a live
        initiator corrupts whatever is using it, while renaming one whose
        export is merely configured is recoverable.

        Note the shape: a **POST** that reads. It takes query filters, so
        the middleware models it as a call rather than a collection, and a
        GET is a 405.

        Each session names its target by full IQN
        (``<basename>:<target name>``), not by id, so a caller correlating
        back to a zvol has to go target name -> target -> targetextent ->
        extent -> ``disk``.

        Returns:
            One dict per session, carrying ``initiator``,
            ``initiator_addr``, ``target`` and ``target_alias`` among
            others. Empty when nothing is connected.
        """
        return self._make_request(
            "POST", "/iscsi/global/sessions", json={})

    def get_iscsi_service(self) -> Dict[str, Any]:
        """
        Get the state of the ``iscsitarget`` service.

        Returns:
            Service record, including ``state`` ("RUNNING"/"STOPPED") and
            ``enable`` (whether it starts at boot)

        Raises:
            TrueNASAPIError: If the appliance reports no such service
        """
        services = self._make_request(
            "GET", "/service", params={"service": "iscsitarget"},
        )
        if not services:
            raise TrueNASAPIError(
                "TrueNAS reported no 'iscsitarget' service. The appliance "
                "may not support iSCSI, or the API contract has changed.",
                method="GET",
                endpoint="/service",
            )
        return services[0]

    def start_iscsi_service(self) -> bool:
        """
        Start the ``iscsitarget`` service.

        On a clean appliance the service is ``STOPPED`` with
        ``enable: false``, and **nothing in the pipeline works until it is
        running** -- a reload does not start it (see
        :meth:`reload_iscsi_service`). This does not make the service
        survive a reboot; that needs ``enable: true`` via
        ``POST /service/update``.

        Returns:
            True if the service is running afterwards
        """
        return bool(self._make_request(
            "POST", "/service/start", json={"service": "iscsitarget"},
        ))

    def reload_iscsi_service(self) -> bool:
        """
        Reload the ``iscsitarget`` service so config changes take effect.

        Target and extent changes are inert until this runs -- which is
        why the client had no working pipeline before #12.

        **A reload does not start a stopped service.** Against a stopped
        service this returns ``False`` and the state stays ``STOPPED``
        (verified in #12), so a caller that only ever reloads will write
        config that never activates and get no error saying so. Check
        :meth:`get_iscsi_service` first.

        Returns:
            True if the reload was performed
        """
        return bool(self._make_request(
            "POST", "/service/reload", json={"service": "iscsitarget"},
        ))

    # ------------------------------------------------------------------
    # Snapshots
    #
    # These live under /pool/snapshot. The legacy /zfs/snapshot path these
    # methods used until #42 is a 404 on 25.10.5 -- see the hazard note in
    # AGENTS.md for why that failed silently rather than loudly.
    #
    # A snapshot's id is `{pool}/{dataset}@{snapshot}` and **must** be
    # percent-encoded into the URL; the raw form 404s.
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_path(snapshot_id: str) -> str:
        """
        Build the URL-encoded path segment for a snapshot id.

        A snapshot id contains both ``/`` and ``@``
        (``Dev-Pool/volume-abc@snap-1``). Interpolated raw, the ``/``
        characters become extra path segments and the appliance answers
        **404** -- verified in #42.

        That 404 is the dangerous kind: `_raise_for_status` maps it to
        :class:`TrueNASAPINotFoundError`, which a caller swallows to make
        deletes idempotent. Before #42 this bug stacked on top of the wrong
        base path, so ``delete_snapshot`` would have reported success on
        every call while deleting nothing, forever.

        Args:
            snapshot_id: Full snapshot id, ``pool/dataset@snapshot``

        Returns:
            Percent-encoded path segment
        """
        return quote(snapshot_id, safe="")

    @staticmethod
    def snapshot_id(pool: str, name: str, snapshot: str) -> str:
        """
        Build a snapshot's id from its parts.

        Args:
            pool: Pool the zvol lives in
            name: Zvol name
            snapshot: Snapshot name, without the ``@``

        Returns:
            Full snapshot id, ``pool/name@snapshot``
        """
        return f"{pool}/{name}@{snapshot}"

    def get_snapshot_list(
        self,
        dataset: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List snapshots, optionally for one dataset only.

        **Pass ``dataset`` unless you genuinely want everything.** The
        unfiltered list includes the appliance's own boot-pool snapshots --
        eight of them on a freshly installed 25.10.5 with no user data --
        so an unfiltered call is both wasteful and easy to misread as
        "these are our snapshots".

        Args:
            dataset: Full dataset path (``pool/name``) to filter by

        Returns:
            List of snapshots. Each carries ``id``, ``name``, ``dataset``,
            ``snapshot_name``, ``pool``, ``type`` and ``properties``.
        """
        if dataset is None:
            return self._make_request("GET", "/pool/snapshot")
        return self._make_request(
            "GET", "/pool/snapshot", params={"dataset": dataset},
        )

    def get_snapshot(self, id: str) -> Dict[str, Any]:
        """
        Get a single snapshot by id.

        Unlike a missing dataset delete, a missing snapshot **GET** answers
        404 rather than 422/errno 2, so it maps to
        :class:`TrueNASAPINotFoundError` through the status branch.

        Args:
            id: Full snapshot id, ``pool/dataset@snapshot``

        Returns:
            Snapshot metadata carrying ``id``, ``name``, ``dataset``,
            ``snapshot_name``, ``pool``, ``createtxg``, ``type`` and
            ``properties``

        Raises:
            TrueNASAPINotFoundError: If no such snapshot exists
        """
        return self._make_request(
            "GET", f"/pool/snapshot/id/{self._snapshot_path(id)}",
        )

    def create_snapshot(
        self,
        dataset: str,
        name: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a new snapshot.

        Creating a snapshot that already exists fails with errno 17
        (EEXIST), *not* ENOENT, so it surfaces as a plain
        :class:`TrueNASAPIError` rather than being mistaken for a missing
        object.

        Args:
            dataset: Dataset path (e.g. ``"tank/volume1"``)
            name: Snapshot name, without the ``@``
            **kwargs: Additional options -- ``recursive``, ``exclude``,
                ``properties``, ``vmware_sync``

        Returns:
            The created snapshot, including its ``id``
        """
        payload = {
            "dataset": dataset,
            "name": name,
            **kwargs
        }
        return self._make_request("POST", "/pool/snapshot", json=payload)

    def rename_snapshot(self, id: str, new_name: str) -> None:
        """
        Rename a snapshot, keeping it on the same dataset.

        The appliance wants the new name in full ``<dataset>@<snapshot>``
        form and rejects a bare snapshot name outright. Since ZFS cannot
        move a snapshot between datasets anyway, this method takes the
        snapshot name alone and composes the full form from the source
        id -- the only correct dataset is the one it already sits on.

        Note the asymmetry with :meth:`rename_zvol`, which is easy to trip
        over: the wrapper key here is ``options``, not ``data``, and a
        missing source is reported as errno **14**, not errno 2, so it
        surfaces as a plain :class:`TrueNASAPIError` rather than
        :class:`TrueNASAPINotFoundError`. Callers that need to distinguish
        "already gone" must check with :meth:`get_snapshot` first.

        ``force`` is mandatory here for the same reason as
        :meth:`rename_zvol`.

        Args:
            id: Full snapshot id to rename, ``pool/dataset@snapshot``
            new_name: New snapshot name, without the ``@``

        Raises:
            TrueNASAPIError: If ``id`` is not a snapshot id, or the
                appliance refused the rename
        """
        dataset, sep, _ = id.rpartition("@")
        if not sep:
            raise TrueNASAPIError(
                f"Not a snapshot id: {id!r}. Expected "
                f"'pool/dataset@snapshot'.",
                method="POST", endpoint="/pool/snapshot/rename")
        self._make_request(
            "POST",
            "/pool/snapshot/rename",
            json={
                "id": id,
                "options": {
                    "new_name": f"{dataset}@{new_name}",
                    "force": True,
                },
            },
        )

    def delete_snapshot(self, id: str, defer: bool = False) -> None:
        """
        Delete a snapshot by ID.

        Deleting a snapshot that does not exist answers 422 with errno 2,
        which maps to :class:`TrueNASAPINotFoundError` -- the same shape as
        a missing dataset, so idempotent deletes work the same way.

        Args:
            id: Full snapshot id, ``pool/dataset@snapshot``
            defer: Defer destruction until the last reference is released.
                Needed when a clone still depends on the snapshot; ZFS
                refuses an immediate destroy in that case. #21 owns the
                clone lifecycle that decides when to set this.
        """
        self._make_request(
            "DELETE",
            f"/pool/snapshot/id/{self._snapshot_path(id)}",
            json={"defer": defer},
        )
