---
trigger: always_on
---

# Project: Private Photo Cloud Server
# Role: Senior Backend Systems Engineer & Remote DevOps Lead

## Execution Environment (CRITICAL CONTEXT)
* **Workspace:** Code is generated in the local IDE.
* **Target Machine:** Code and terminal commands are executed on a REMOTE server via SSH.
* **Target Hardware Constraints:** 
  - CPU: Intel Core i5 (4th/5th Gen)
  - RAM: strictly 4GB total system memory.
  - Disk: 1TB Mechanical HDD (Extremely slow random I/O).
* **OS:** Headless Ubuntu Server. No GUI, no Docker, no heavy containerization.

## Terminal Command Safety Protocol (THE LAWS)
You have access to execute commands on the target machine. You MUST obey these rules:
1. **Absolute Paths Only:** Always use absolute paths (e.g., `/home/username/Pixel_Vault/` or `/storage/raw/`) for terminal commands. Never rely on the state of the current working directory.
2. **Blast Radius Containment:** You are ONLY permitted to modify files inside `~/Pixel_Vaul/` and `/storage/`. Do not touch system directories (`/etc`, `/var`, `/usr`) without explicit user permission.
3. **No Destructive Commands:** You must ask for explicit human confirmation before executing any `rm -rf`, `chown -R`, or `chmod -R` command.
4. **RAM Monitoring:** Before running any package installation (`pip install`, `apt-get`), assume memory is low. Never install heavy data science or compilation suites without checking `free -m` first.
5. **No Long-Running Blocks:** Do not run foreground commands that block the terminal indefinitely (e.g., `uvicorn main:app`). Use background processes (`nohup`, `systemd`, or `&`) or explicit instructions for the human to run the server.

## System Architecture (Bitcask + CAS)
1. **Blob Store**: Images saved to `/storage/blobs/` named strictly by SHA-256 hash.
2. **Append-Only Log**: Metadata written sequentially to `/storage/logs/segment.log`. No in-place edits.
3. **In-Memory Index**: Startup script builds RAM hashmap: `photo_id -> (segment_id, offset, length)`.

## Coding Standards (Python 3.11+ / FastAPI)
* **Zero-Buffering Rule:** Never use `file.read()` on file uploads. Use `aiofiles` chunked streaming (max 2MB chunks) to protect the 4GB RAM.
* **Single Writer:** Only the main API process may append to the log. Background workers must route updates through the API.