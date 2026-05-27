# Security Hardening Implementation

This document covers the security hardening roles added to the BrieHost provisioning workflow: filesystem security, PHP hardening, and automatic updates with auditing.

## Overview

Three hardening areas are now enforced on the PHP template during `setup_php_template.yml`:

1. **`hardening_os`** - OS-level security (filesystem, auto-updates, auditd)
2. **`hardening_php`** - PHP & web server security (disable dangerous functions, harden php-fpm)
3. Applied once to the template; inherited by all cloned tenant containers

---

## 1. Filesystem Hardening (`hardening_os`)

### Mount Options
- `/var/www/html` mounted with `noexec,nosuid,nodev` to prevent:
  - Execution of binaries uploaded by users
  - SUID bit exploitation
  - Device files
- Made persistent via `/etc/fstab`

### Secure Directories
- `/var/tmp/php-sessions` - sticky-bit 1700 (www-data only)
- `/var/tmp/php-uploads` - sticky-bit 1700 (www-data only)
- Web root owned by `www-data` with 755/644 permissions

### Automatic Security Updates
- **unattended-upgrades** installed and configured
- Applies security patches automatically (`-security` sources only)
- Mail notifications on changes (to root)
- Prevents disruption from routine security updates

### Audit Logging (auditd)
Monitors:
- `/var/www/html` for file/attribute changes (`-k web_changes`)
- `/etc/nginx/` and `/etc/php/` for config changes
- `/etc/passwd`, `/etc/shadow`, `/etc/group` for user changes

Logs to `/var/log/audit/audit.log` with daily rotation (10-day retention).

### Kernel Hardening
Sysctl parameters configured:
- ICMP redirects disabled (prevent MITM)
- SYN cookies enabled (TCP flood protection)
- Reverse path filtering enabled
- Kernel pointer exposure restricted

---

## 2. PHP Security Hardening (`hardening_php`)

### Dangerous Functions Disabled
```
exec, system, passthru, shell_exec, proc_open, proc_close, 
popen, pclose, eval, assert, pcntl_*
```
Configured via `/etc/php/8.2/mods-available/security.ini` and loaded globally.

### PHP.ini Hardening
| Setting | Value | Reason |
|---------|-------|--------|
| `expose_php` | Off | Hide PHP version |
| `display_errors` | Off | Don't leak info to browsers |
| `log_errors` | On | Log errors to file instead |
| `short_open_tag` | Off | Force `<?php` only |
| `upload_tmp_dir` | `/var/tmp/php-uploads` | Isolated, noexec mount |
| `session.save_path` | `/var/tmp/php-sessions` | Isolated, sticky-bit |
| `session.cookie_httponly` | 1 | Prevent JS theft |
| `session.cookie_secure` | 1 | HTTPS only (reverse proxy handles) |
| `session.cookie_samesite` | Lax | CSRF protection |

### PHP-FPM Hardening
Pool config `/etc/php/8.2/fpm/pool.d/www-hardened.conf`:
- Listen on socket only (`/run/php/php-fpm.sock`, mode 0660)
- No TCP ports exposed
- Dynamic process management (min/max workers)
- `max_requests=1000` to prevent memory leaks
- Request timeout: 30s (configurable per app)
- Catch worker output for logging
- Slow request logging (10s threshold)

### Nginx Security Headers
Automatically injected into sites:
```nginx
X-Frame-Options: SAMEORIGIN         # Clickjacking protection
X-XSS-Protection: 1; mode=block      # Legacy XSS filter
X-Content-Type-Options: nosniff      # MIME-sniffing protection
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(), microphone=(), geolocation=()
server_tokens: off                   # Hide Nginx version
```

### Log Management
- PHP error log: `/var/log/php-errors.log`
- PHP-FPM slow log: `/var/log/php-fpm/www-slow.log`
- Rotation: daily, 10-day retention, compressed
- Owned by `www-data` with 640 permissions

---

## 3. Automatic Updates & Auditing

### Unattended-Upgrades Configuration
File: `/etc/apt/apt.conf.d/50unattended-upgrades`

```ini
# Only security updates from Debian
Unattended-Upgrade::Allowed-Origins {
  "${distro_id}:${distro_codename}-security";
};

# Minimal downtime
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::InstallOnShutdown "true";

# Notifications
Unattended-Upgrade::Mail "root";
Unattended-Upgrade::MailReport "on-change";
```

File: `/etc/apt/apt.conf.d/20auto-upgrades`
- Checks for updates daily
- Downloads security patches daily
- Applies them automatically

### Audit Rules
File: `/etc/audit/rules.d/web.rules`

Watches for unauthorized changes:
```
-w /var/www/html -p wa -k web_changes         # Site files
-w /etc/nginx/ -p wa -k config_changes        # Web config
-w /etc/php/ -p wa -k config_changes          # PHP config
-w /etc/passwd -p wa -k users                 # User changes
```

View audit logs:
```bash
ausearch -k web_changes              # Recent file changes
ausearch -k config_changes           # Configuration changes
ausearch -k users                    # User/group changes
```

---

## Usage

### Initial Template Setup
Apply hardening during template creation:

```bash
ansible-playbook -i infra/ansible/inventory/production.ini \
  infra/ansible/playbooks/setup_php_template.yml \
  -e template_vmid=<VMID>
```

The playbook now runs in this order:
1. Install PHP extensions + Composer (existing)
2. Harden OS (filesystem, updates, auditd)
3. Harden PHP (disable functions, php-fpm, headers)

### Verify Hardening

**Inside template CT, after setup:**

```bash
# Check mount options
mount | grep /var/www/html
# Expected: rw,nosuid,nodev,noexec

# Check disabled functions
php -i | grep disable_functions
# Expected: exec,system,passthru,shell_exec,...

# Check unattended-upgrades is active
systemctl status unattended-upgrades

# View audit rules
auditctl -l

# Test PHP-FPM
systemctl status php8.2-fpm
```

### Per-Tenant Inheritance

Tenant containers cloned from this template automatically inherit:
- Secure mount options
- PHP restrictions
- Automatic security updates
- Audit monitoring
- Security headers

---

## Monitoring & Maintenance

### Check for Security Updates
```bash
# Inside template/tenant CT
apt list --upgradable | grep security

# Unattended-upgrades log
cat /var/log/unattended-upgrades/unattended-upgrades.log
```

### Review Audit Logs
```bash
# Recent file changes in web root
ausearch -k web_changes --format text | tail -20

# Configuration changes
ausearch -k config_changes --format text | tail -20

# Generate report
aureport --key-summary
```

### PHP Error Logging
```bash
# Real-time PHP errors
tail -f /var/log/php-errors.log

# Slow requests (>10s)
tail -f /var/log/php-fpm/www-slow.log
```

---

## Security Considerations

### What's Protected
✓ Prevents arbitrary PHP execution via `exec()`, `system()`, etc.
✓ Web uploads cannot become executable
✓ Configuration changes logged & searchable
✓ Security patches applied automatically
✓ Session hijacking hardened (HttpOnly, Secure, SameSite)
✓ Information disclosure reduced (error reporting, headers)

### What's NOT Protected
✗ DoS attacks (rate-limiting via reverse proxy/Cloudflare)
✗ SQL injection (developer responsibility)
✗ XSS in application code (developer responsibility)
✗ Tenant-to-tenant network attacks (handled by OPNsense firewall)
✗ Malware in uploaded zips (mitigated by ClamAV + YARA signatures, but not a full sandbox/detonation pipeline)

### Multi-Tenant Isolation
- Network isolation via OPNsense (different bridge)
- Filesystem isolation via LXC unprivileged containers
- Audit trails per-container (via site metadata in logs)
- No cross-container capability to escalate or escape

---

## Future Enhancements

Potential additions (not implemented):
- **AppArmor/seccomp profiles** - per-role confinement for nginx/php-fpm
- **fail2ban** - auto-block repeated 4xx/5xx errors
- **ClamAV integration** - on-access scanning of `/var/www/html`
- **WAF rules** - ModSecurity for nginx
- **Centralized logging** - syslog-ng to log collector
