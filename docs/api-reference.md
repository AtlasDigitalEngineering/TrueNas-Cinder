# TrueNAS API reference

Which TrueNAS Scale endpoints this driver uses, what it needs from them, and
the behaviours that are not what the API documentation implies.

**What is authoritative.** The live appliance, and after it
`tests/functional/`, which re-checks every finding below against real hardware.
This document records the *shape* and the *traps*; it deliberately does not
restate payloads, because a second copy of an undocumented API's behaviour is
the kind of thing that goes quietly out of date, and the entire value of these
findings is that somebody confirmed them.

Verified against **TrueNAS-25.10.5**. A different release may differ — that is
what the functional suite is for.

## Authentication

`Authorization: Bearer <api_key>`, against `/api/v2.0`. The client accepts a
base URL with or without the suffix.

**Never put credentials in the URL.** `https://user:pass@appliance` does not
work: `requests` converts the userinfo into a Basic header that overwrites the
Bearer key, so every call 401s — and it keeps the userinfo in `response.url`,
which lands in tracebacks. The client rejects such a URL outright.

## The API key needs `FULL_ADMIN`

Least privilege was attempted and does not work on this release. A key whose
account holds only the eleven roles the driver's calls map to is refused with
HTTP 403; the same key with `FULL_ADMIN` succeeds. Verified A-B-A. The eleven
are listed in [configuration.md](configuration.md), for retrying on a future
release.

The two auth failures need opposite fixes, so the client distinguishes them:

| Status | Meaning | Remedy |
|---|---|---|
| `401` | Key is wrong, revoked or expired | Issue a new key |
| `403` | Key is valid; the account lacks the role | Grant `FULL_ADMIN` — do **not** reissue |

## Endpoints used

Datasets and snapshots:

| Endpoint | Used for |
|---|---|
| `GET /pool` | Capacity for the scheduler; pool existence at startup |
| `GET`, `POST`, `DELETE /pool/dataset[/id/{id}]` | Zvol lifecycle |
| `POST /pool/dataset/rename` | Adoption and release — rename in place, no data copy |
| `POST /pool/dataset/id/{id}/promote` | Never called by the driver; see below |
| `GET`, `POST`, `DELETE /pool/snapshot[/id/{id}]` | Snapshot lifecycle |
| `POST /pool/snapshot/rename` | Snapshot adoption |
| `POST /pool/snapshot/clone` | Clone and create-from-snapshot |

iSCSI:

| Endpoint | Used for |
|---|---|
| `GET /iscsi/global` | Target base name (IQN prefix) and port |
| `POST /iscsi/global/sessions` | Live sessions — the adoption safety gate |
| `GET`, `POST /iscsi/portal` | Portal discovery; creation only in tests |
| `GET`, `POST /iscsi/initiator` | Initiator groups, one per connector IQN |
| `GET`, `POST`, `DELETE /iscsi/extent[/id/{id}]` | Extent per volume |
| `GET`, `POST`, `PUT`, `DELETE /iscsi/target[/id/{id}]` | Target per volume |
| `POST /iscsi/target/validate_name` | Checked at startup against `volume_name_template` |
| `GET`, `POST`, `DELETE /iscsi/targetextent[/id/{id}]` | Target-to-extent link |

Service:

| Endpoint | Used for |
|---|---|
| `GET /service` | `iscsitarget` state and `enable` at startup |
| `POST /service/start` | Never called by the driver; tests only |
| `POST /service/reload` | After every export change, to make it live |
| `GET /truenas/is_eula_accepted` | Available on the client; not called by the driver |

**Setup validates and reports; it never changes the appliance.** The driver
does not create a portal, does not start the iSCSI service, and does not
promote a clone — `create_portal`, `start_iscsi_service`, `promote_clone` and
`is_eula_accepted` exist on the client but no driver path calls them. Three of
the four are used only by the functional suite, which has to provision a clean
appliance to test against.

## Behaviours worth knowing

Each of these contradicted an initial assumption and is asserted by the
functional suite, so a change in a future TrueNAS release fails loudly rather
than silently invalidating the client.

**"Object not found" has two forms.** Some endpoints answer 404 with
`{"message": ""}`; others answer 422 with an errno inside a structured body.
The client maps both to `TrueNASAPINotFoundError`.

**Match on `errno`, never on message text.** Creating into a nonexistent pool
returns errno 22 with a message that reads like a not-found. The distinction
between "absent" and "invalid" is only reliable in the errno.

**Two different error body shapes.** Most validation errors use a keyed form,
`{<field>: [{"message": …, "errno": …}]}`; the two rename endpoints return a
flat `{"message": …, "errno": …}` instead. Both must be parsed.

**Unknown filter fields fail open.** A query filtering on a field the endpoint
does not know answers `200` with an empty list rather than rejecting it. A
typo in a filter therefore reads as "nothing found" — which is why the
adoption safety gate reads whole collections rather than filtering server-side.

**Deleting either end of a target-extent link cascades the link.** The design
spec said TrueNAS does not cascade. It does, and `remove_export` relies on it.
Deleting a target leaves the *extent* in place, so both must be removed.

**Never send the destructive delete options.** `DELETE /iscsi/target` accepts
`delete_extents`, `DELETE /iscsi/extent` accepts `remove`, and both accept
`force`. All default to false and the client sends none of them.

**Portal ordering is not preserved.** Posting groups for portals `[11, 12]`
returns them as `[12, 11]`. The first address in `provider_location` becomes
the singular `target_portal` a non-multipath connector uses, so the driver
builds that order from configuration and never reads it back from the
appliance.

**Initiator groups have no uniqueness constraint.** Nothing stops two groups
holding the same IQN, so deduplication happens on the client side — and it
races. Six concurrent creates produced six groups, measured; the driver
serialises the lookup for this reason.

**`promote` moves the snapshot; it does not sever the dependency.** Promoting a
clone reverses the parent/child relationship and relocates the origin snapshot
to the promoted dataset. It does not make a clone independent in the way the
name suggests, so the driver never promotes: doing so would change what a later
delete removes.

**A reload does not start a stopped service.** `POST /service/reload` on a
stopped `iscsitarget` succeeds and leaves it stopped. Every export built while
it is stopped is accepted and inert.

**A second extent on the same zvol is refused**, and extent names must be
unique. Both are relied on by the export path's idempotency.

## Error mapping

Every failure raised by the client is a `TrueNASAPIError` subclass, including
network errors, so the driver translates with a single `except` and never sees
a raw `requests` exception.

Requests carry a default `(10s, 60s)` timeout. HTTP 429 and 503 are retried
with backoff; **timeouts are deliberately not retried** — a read timeout does
not mean the appliance stopped working on the request, so replaying a create or
delete on top of one is worse than failing.
