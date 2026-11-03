# DNS lab module

Consumed by `infra/envs/dev/azure-dns`, which supplies pinned providers and delete-OS-disk behavior. Names, regions, CIDRs, roles, SKU, disk, image and normal Traffic Manager monitor settings are fixed; only owned subscription, public SSH/WG source /32, public SSH key, reviewed TTL and bounded hourly tags are inputs. The matching strict scope adapter admits no update/replacement and preserves the independent Task3 guard.

Outputs are inventory for private controller files and the exact owned A-record values. See the env README for pricing, lifecycle, guest bootstrap, three-origin measurement and cleanup. The module creates no shared resource group, role or backend.
