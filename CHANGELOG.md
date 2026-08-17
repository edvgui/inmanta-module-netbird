# Changelog

## v0.1.1 - ?

- Manage the groups of a netbird account with the `netbird::Group` resource
- Manage the setup keys of a netbird account with the `netbird::SetupKey` resource
- Manage the peers that joined a netbird account with the `netbird::Peer` resource
- Manage the networks of a netbird account, the addresses they give access to and the
  peers routing towards them, with the `netbird::Network`, `netbird::NetworkResource`
  and `netbird::NetworkRouter` resources
- Manage the nameserver groups of a netbird account with the
  `netbird::NameserverGroup` resource, and its dns settings with the
  `netbird::DnsSettings` resource
- Manage the dns zones of a netbird account with the `netbird::DnsZone` resource, and
  the records they hold with the `netbird::DnsZoneRecord` resource

## v0.1.0 - 2026-08-11

- Initial module structure
- Manage the users and service users of a netbird account with the `netbird::User`
  resource
- Manage the groups of a netbird account with the `netbird::Group` resource
