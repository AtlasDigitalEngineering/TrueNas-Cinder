# Configuring the TrueNAS Cinder driver

## Appliance prerequisites

The driver **validates** these at startup and refuses to run without them. It
never creates or changes them: they are appliance-wide, shared by every volume
and every `cinder-volume` worker, so provisioning a volume must not have them as
a side effect.

1. **An API key for a service account.** Credentials → API Keys in the TrueNAS
   UI. The driver authenticates with a Bearer key, not a username and password.
2. **A ZFS pool** for volumes. Volumes are created in it as zvols.
3. **The iSCSI service running, and enabled at boot.** System Settings →
   Services → iSCSI. If it is stopped the driver refuses to start — with good
   reason: the appliance accepts target and extent configuration while the
   service is down, reports no error, and nothing attaches. If it is running but
   not enabled, the driver warns: it works now and breaks at the next reboot.
4. **An iSCSI portal bound to a statically configured address.** Shares → Block
   Shares (iSCSI) → Portals. TrueNAS offers only static addresses as portal
   addresses — a DHCP interface is not selectable at all. A portal bound to
   `0.0.0.0` is accepted by the appliance but is not an address a compute node
   can connect to, so the driver requires `truenas_iscsi_portal_addresses` in
   that case.

## Sample backend section

```ini
[DEFAULT]
enabled_backends = truenas-iscsi

[truenas-iscsi]
volume_driver = truenas_cinder_driver.driver.TrueNASISCSIDriver
volume_backend_name = truenas-iscsi

# Required.
truenas_api_url = https://truenas.example.com
truenas_api_key = <service account API key>
truenas_pool = tank

# Only needed when the appliance has more than one portal. With exactly one,
# the driver discovers it and logs which it chose.
#truenas_iscsi_portal_id = 1

# Addresses initiators should use, in preference order. Required when the
# portal binds 0.0.0.0 or ::. Listing more than one advertises multipath:
# the driver binds the target to every portal and Cinder presents them as
# target_portals / target_iqns / target_luns.
#truenas_iscsi_portal_addresses = 10.20.21.81,10.40.96.182

# Leave enabled. Fix the appliance certificate rather than turning this off.
truenas_verify_ssl = true
```

`truenas_api_key` is declared `secret=True`, so oslo_config redacts it from
logged configuration dumps. It is still plain text in `cinder.conf` — protect
that file as you would any other credential store.

## Options

| Option | Type | Default | Required |
|---|---|---|---|
| `truenas_api_url` | string | — | yes |
| `truenas_api_key` | string (secret) | — | yes |
| `truenas_pool` | string | — | yes |
| `truenas_iscsi_portal_id` | integer | discovered | only with several portals |
| `truenas_iscsi_portal_addresses` | list | portal's own addresses | when the portal binds a wildcard |
| `truenas_verify_ssl` | boolean | `true` | no |

## Volume naming

Cinder's `volume_name_template` becomes the iSCSI target name. TrueNAS accepts
only **lowercase alphanumerics plus `.`, `-` and `:`**, so the default
`volume-%s` is fine but a template with an underscore or a capital is not. The
driver validates the rendered name against the appliance at startup rather than
letting it fail at the first attach.

## Options this driver does not use

`san_ip`, `san_login`, `san_password` and `san_private_key` come from the
`SanISCSIDriver` base class and drive arrays over SSH. This driver only ever
speaks to the REST API, so they are unused and need not be set — its
`check_for_setup_error` deliberately does not call the base class
implementation, which would otherwise demand them.

Older TrueNAS driver notes referenced `iscsi_ip_address`, `iscsi_protocol` and
`target_prefix` in this section. The first two no longer exist in OpenStack
2025.1 — the equivalents are `target_ip_address` and `target_protocol` — and
none of the three is needed: the driver reads the portal address from the
appliance (or from `truenas_iscsi_portal_addresses`) and the IQN prefix from
`GET /iscsi/global`.
