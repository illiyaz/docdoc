# PORTS.md — Forentis AI Port Assignments

This file documents all ports reserved by Forentis AI.
**Do not use ports 3847–3854 for any other product or service on shared infrastructure.**

Last updated: 2026-02-23

---

## Reserved Port Range: 3847–3854

| Port | Service | Protocol | Description |
|------|---------|----------|-------------|
| **3847** | Frontend (React) | HTTP | Human review UI — queue dashboard, subject detail, approval workflow |
| **3848** | FastAPI Backend | HTTP | REST API + `/docs` (Swagger UI) |
| **3849** | PostgreSQL | TCP | Primary database — NotificationSubjects, AuditEvents, ReviewTasks |
| **3850** | Redis | TCP | Cache and session store |
| **3851** | MinIO API | HTTP | Object storage API (S3-compatible) |
| **3852** | MinIO Console | HTTP | MinIO web admin UI |
| **3853** | Mailpit SMTP | SMTP | Local mail relay — catches all outgoing notification emails |
| **3854** | Mailpit Web UI | HTTP | Email preview UI for dev/demo — shows rendered notification emails |

---

## Quick Reference

| What you want to access | URL |
|---|---|
| Review UI (human reviewers) | http://localhost:3847 |
| API documentation (Swagger) | http://localhost:3848/docs |
| API health check | http://localhost:3848/health |
| Sent notification emails | http://localhost:3854 |
| MinIO file storage | http://localhost:3852 |

---

## Environment Config

In `.env` or `docker-compose.yml`, use these values:

```env
API_URL=http://localhost:3848
DATABASE_URL=postgresql://notifai:notifai@localhost:3849/notifai
REDIS_URL=redis://localhost:3850
MINIO_URL=http://localhost:3851
SMTP_HOST=localhost
SMTP_PORT=3853
```

---

## Why This Range

- Avoids all standard well-known ports (80, 443, 5432, 6379, 8080, 9000)
- Avoids common dev defaults (3000, 3001, 4000, 5000, 8000, 8001)
- All 8 ports are contiguous — easy to firewall as a single range
- Range is memorable: starts at 3847, ends at 3854

---

## Firewall Rule (if needed)

To allow the full application through a firewall as one rule:

```bash
# UFW (Ubuntu)
ufw allow 3847:3854/tcp

# iptables
iptables -A INPUT -p tcp --match multiport --dports 3847:3854 -j ACCEPT

# AWS Security Group — inbound rule
Port range: 3847 – 3854
Protocol: TCP
```

For production, only expose **3847** (frontend) and **3848** (API) externally.
Ports 3849–3854 should be internal-only.

---

## Production vs. Dev Exposure

| Port | Dev (local) | Staging | Production |
|------|-------------|---------|------------|
| 3847 Frontend | ✅ Open | ✅ Open | ✅ Open (behind TLS proxy) |
| 3848 API | ✅ Open | ✅ Open | ✅ Open (behind TLS proxy) |
| 3849 PostgreSQL | ✅ Open | ❌ Internal only | ❌ Internal only |
| 3850 Redis | ✅ Open | ❌ Internal only | ❌ Internal only |
| 3851 MinIO API | ✅ Open | ❌ Internal only | ❌ Internal only |
| 3852 MinIO Console | ✅ Open | ❌ Internal only | ❌ Internal only |
| 3853 Mailpit SMTP | ✅ Open | ❌ Internal only | ❌ Replace with production SMTP relay |
| 3854 Mailpit UI | ✅ Open | ❌ Internal only | ❌ Not deployed in production |

---

## Other DCube Products — Do Not Use This Range

If you are building a different product under the DCube umbrella,
choose a port range that does not overlap with 3847–3854.

Suggested ranges for other products:
- 3855–3862 (next contiguous block)
- 3900–3907
- 4000–4007

Update this file with a cross-reference if another product
is assigned a nearby range.

---

## Contact

Questions about port assignments: contact the Forentis AI team before
deploying any service on shared infrastructure that uses ports in or near this range.