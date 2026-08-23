# CommunityAI GCP bootstrap

This deployment runs only the lightweight Hivemind discovery peer. It serves no
models, receives no prompts, and has no Google Cloud API identity.

The production instance uses an `e2-micro`, a standard persistent disk, a stable
regional IPv4 address, a dedicated VPC, public TCP 31337, and SSH restricted to
Google IAP. The instance's peer identity persists in `/var/lib/communityai`.

Install after copying this directory to the VM:

```bash
sudo ./install_bootstrap.sh PUBLIC_IPV4
```

The complete `/ip4/.../tcp/31337/p2p/...` address is written to the systemd
journal. The private identity file must never leave the VM.

## Live deployment

- Project: `community-ai-506321`
- VM: `communityai-bootstrap-1` in `us-central1-a`
- Public IPv4: `35.209.21.129`
- Peer address: `/ip4/35.209.21.129/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo`
- Proposed DNS: `bootstrap.communityai.flujo.com.co` (`A` record to `35.209.21.129`)

The VM has no service account. SSH is allowed only through Google IAP; TCP
31337 is the only application port exposed publicly.
