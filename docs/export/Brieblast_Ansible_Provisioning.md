# Systeemdocumentatie: Brieblast Ansible Provisioning

**Doel:** volledige uitleg van de Ansible-playbooks, roles en taken die de Brieblast provisioning- en gateway-infrastructuur aansturen.

---

## 1. Overzicht

De Python-API provisioneert tenant-sites niet rechtstreeks via losse Proxmox-calls. De worker runt Ansible-playbooks zodat de infrastructuurlogica versieerbaar, herhaalbaar en beter uitlegbaar blijft.

Belangrijke playbooks:

| Playbook | Doel |
| --- | --- |
| `infra/ansible/playbooks/provision_site.yml` | nieuwe tenant-CT provisioneren en site deployen |
| `infra/ansible/playbooks/delete_site.yml` | tenant-CT opruimen voor een site |
| `infra/ansible/playbooks/setup_gateway.yml` | gateway-CT inrichten |
| `infra/ansible/playbooks/setup_php_template.yml` | PHP-template hardenen en voorbereiden |

De API gebruikt in normale runtime vooral `provision_site.yml` en `delete_site.yml`.

---

## 2. Hoe de API Ansible aanroept

`app/worker.py` bouwt bij provisioning deze extra vars op:

- `site_id`
- `user_id`
- `site_slug`
- `zip_path`
- `target_node`
- `template_vmid`

Daarna draait de worker:

```bash
ansible-playbook <playbook> -i <inventory> -e '<json extra vars>'
```

Bij delete draait de worker `delete_site.yml` met minstens:

- `site_id`

Time-out en paden zijn configureerbaar via:

- `ANSIBLE_PLAYBOOK_PATH`
- `ANSIBLE_DELETE_PLAYBOOK_PATH`
- `ANSIBLE_INVENTORY_PATH`
- `ANSIBLE_TIMEOUT_SECONDS`
- `ANSIBLE_EXTRA_VARS_JSON`

---

## 3. Inventory en group vars

### 3.1 `group_vars/proxmox.yml`

Deze file levert de standaardwaarden voor tenantprovisioning:

| Variabele | Betekenis |
| --- | --- |
| `proxmox_api_host` | Proxmox-host uit environment |
| `proxmox_api_user` | Proxmox-user uit environment |
| `proxmox_api_token_id` | token-id uit environment |
| `proxmox_api_token_secret` | token-secret uit environment |
| `tenant_pool` | Proxmox pool, standaard `briehost` |
| `tenant_bridge` | bridge voor tenant-netwerk, standaard `vmbr1` |
| `tenant_disk_storage` | storage backend, standaard `local-lvm` |
| `tenant_disk_gb` | grootte rootfs |
| `tenant_cores` | aantal CPU-cores |
| `tenant_memory_mb` | RAM |
| `tenant_swap_mb` | swap |
| `tenant_hostname` | hostname afgeleid van `site_slug` en UUID-fragment |

### 3.2 `group_vars/gateway.yml`

Deze file bevat de gatewaydefaults:

- `cloudflared_tunnel_token`
- `gateway_admin_bind_ip`
- `gateway_admin_allowed_source`
- `gateway_domain`

---

## 4. Provisioning-playbook stap voor stap

`provision_site.yml` draait op hostgroep `proxmox` en heeft deze globale opbouw:

1. pre_tasks valideren input;
2. daarna volgen de roles:
   - `proxmox_cleanup`
   - `proxmox_clone`
   - `start_container`
   - `deploy_site_zip`
   - `healthcheck`

### 4.1 Pre-tasks in `provision_site.yml`

#### Taak: validate required extra-vars + Proxmox API credentials

Controleert dat alle verplichte inputs bestaan:

- `site_id`
- `user_id`
- `zip_path`
- `target_node`
- `template_vmid`
- alle Proxmox API credentials

Zonder deze check zou de playbook pas later en vager falen.

#### Taak: verify zip exists on the controller

Doet een `stat` op `zip_path` op `localhost`, dus op de machine waar Ansible gestart is. In deze architectuur is dat de API-container of hostcontext waar de worker draait.

#### Taak: reject missing/non-regular zip paths

Faalt expliciet als `zip_path` niet bestaat of geen gewoon bestand is.

---

## 5. Role `proxmox_cleanup`

Doel: resten van een vorige mislukte provisioningrun voor dezelfde `site_id` verwijderen.

### Taken

#### Find leftover tenant CTs for this site_id

Loopt over `pct list`, opent van elke CT de configuratie en zoekt in `description` naar `site_id=<uuid>`.

Waarom op `description` matchen:

- dat is het eigendomslabel dat de eigen provisioning zet;
- zo wordt niets verwijderd dat niet door Brieblast aangemaakt is.

#### Stop leftover tenant CTs (best-effort)

Probeert elke gevonden CT te stoppen met `pct stop`. `failed_when: false` betekent dat een reeds gestopte of half-kapotte CT de cleanup niet blokkeert.

#### Destroy leftover tenant CTs

Voert `pct destroy <vmid> --purge --force` uit op alle gevonden CT's.

#### Cleanup summary

Schrijft een debugsamenvatting uit zodat de playbook-output achteraf duidelijk maakt of er effectief opgeruimd moest worden.

---

## 6. Role `proxmox_clone`

Doel: een nieuwe tenant-CT klonen uit het PHP-template en basisresources instellen.

### Taken

#### Pick the next free VMID

Gebruikt:

```bash
pvesh get /cluster/nextid
```

Dat levert een vrij VMID op.

#### Set new_vmid fact

Slaat het gekozen VMID op in Ansible-fact `new_vmid`.

#### Clone PHP template -> tenant CT

Gebruikt de `community.proxmox.proxmox` module om:

- `template_vmid` te full-clonen;
- storage `tenant_disk_storage` te gebruiken;
- pool `tenant_pool` te zetten;
- hostname `tenant_hostname` te geven.

Belangrijke nuance:

- de taak heeft retries;
- er wordt expliciet gewacht en opnieuw geprobeerd bij typische lock-conflicten zoals `already locked` of `VM already exists`.

#### Configure tenant CT resources + network

Deze taak zet de runtimeconfig van de nieuwe CT:

- `cores`
- `memory`
- `swap`
- `onboot: true`
- `net0` op `tenant_bridge` met `ip=dhcp`
- `description` met site-, user- en hostinformatie

Die `description` is later cruciaal voor cleanup en voor zichtbaarheid in de Proxmox UI.

#### Grow root disk to `<tenant_disk_gb>G`

Voert `pct resize` uit op `rootfs`.

De taak behandelt twee niet-fatale gevallen correct:

- disk is al groot genoeg;
- shrinken is niet mogelijk en ook niet gewenst.

---

## 7. Role `start_container`

Doel: de nieuw gekloonde CT effectief starten en een bruikbaar tenant-IP verkrijgen.

### Taken

#### Start tenant CT

Gebruikt opnieuw de `community.proxmox.proxmox` module, nu met `state: started`.

#### Wait until pct exec works inside the tenant CT

Probeert herhaaldelijk:

```bash
pct exec <vmid> -- /bin/true
```

Deze stap wacht dus niet alleen tot Proxmox de CT als "running" ziet, maar tot processen in de container effectief bereikbaar zijn.

#### Wait until tenant CT has a DHCP lease on eth0

Leest in de CT via `ip -4 -o addr show dev eth0` tot er een IPv4-adres beschikbaar is.

#### Record tenant IP

Slaat het gevonden IP op als fact `tenant_ip`.

#### Update CT description with the assigned IP

Schrijft een bijgewerkte `description` terug naar Proxmox zodat operators in de UI meteen het tenant-IP zien.

#### Prime upstream ARP/state from inside the CT

Voert vanuit de CT een ping en DNS-opzoeking uit.

Doel:

- OPNsense en bridge laten het MAC/IP-pad leren;
- vermijden dat de eerste inbound connectie faalt door laattijdige ARP-resolutie.

Deze stap is best-effort en mag niet falen.

---

## 8. Role `deploy_site_zip`

Doel: de gevalideerde ZIP effectief in de tenant-CT krijgen en uitrollen.

### Taken

#### Stage zip on Proxmox host

Kopieert de ZIP vanuit `zip_path` naar een tijdelijke locatie op de host:

```text
/tmp/briehost-<site_id>.zip
```

#### Stage canonical deploy script on Proxmox host

Kopieert `infra/templates/deploy-site.sh` naar `/tmp/briehost-deploy-site.sh`.

Dat is belangrijk: ook als het template al een script bevat, wordt telkens de canonieke versie uit de repo gebruikt.

#### Push staged zip into tenant CT

Gebruikt `pct push` om de ZIP in de CT te plaatsen als `/tmp/site.zip`.

#### Push canonical deploy script into tenant CT

Overschrijft `/usr/local/bin/deploy-site.sh` in de tenant-CT.

Zo weet de playbook zeker dat de deploylogica overeenkomt met de code in versiebeheer.

#### Run in-CT deploy script

Voert uit:

```bash
pct exec <vmid> -- bash /usr/local/bin/deploy-site.sh /tmp/site.zip
```

Dit is de belangrijkste uitrolstap en daar gebeurt het echte websitewerk.

#### Clean up staged files on host

Verwijdert de tijdelijke hostbestanden.

#### Clean up zip inside tenant CT

Verwijdert `/tmp/site.zip` in de tenant-CT.

---

## 9. Wat `deploy-site.sh` exact doet

Dit script draait binnen de tenant-CT en bepaalt dus hoe een site effectief live komt.

### 9.1 Basisuitrol

1. ZIP wordt uitgepakt in een tijdelijke stage-directory.
2. Als de ZIP exact één top-level map bevat, wordt die map automatisch "flattened".
3. De bestaande webroot `/var/www/html` wordt pas gewist nadat unzip geslaagd is.
4. Daarna worden de nieuwe bestanden naar de webroot verplaatst.
5. Ownership gaat naar `www-data:www-data`.
6. Directory-permissies worden `755`, files `644`.

### 9.2 Detectie van Laravel

Laravel wordt herkend als:

- `artisan` aanwezig;
- `composer.json` aanwezig;
- `public/` aanwezig.

Bij Laravel doet het script extra werk:

1. gecachte Laravel-configbestanden verwijderen;
2. `.env` aanmaken vanuit `.env.example` of een minimale fallback;
3. `composer install --no-dev` draaien als `www-data` als `vendor/` nog ontbreekt;
4. indien Vite gedetecteerd wordt en nog geen build aanwezig is:
   - `npm ci` of `npm install`
   - `npm run build`
   - `node_modules` weer verwijderen;
5. `APP_KEY` genereren indien nodig;
6. SQLite afdwingen als databasebackend;
7. `database/database.sqlite` aanmaken;
8. opslag- en cachemappen schrijfbaar maken;
9. `artisan config:clear`, `cache:clear`, `route:clear`, `view:clear`;
10. `artisan migrate --force` proberen als er migraties bestaan.

Belangrijk detail: migratiefouten stoppen de volledige deploy niet. Dat is een bewuste keuze zodat een site nog kan booten zelfs als de appmigraties incompleet of incompatibel zijn.

### 9.3 Nginx-vhost herschrijven

Het script genereert ook de effectieve Nginx-siteconfig:

- Laravel krijgt `root <webroot>/public` en Laravel-`try_files`;
- gewone PHP-sites krijgen `root /var/www/html` en een eenvoudige static/PHP-config.

Daarna volgen:

- `nginx -t`
- `systemctl reload nginx` of fallback restart
- reload van alle `php*-fpm` services om opcache te verversen

---

## 10. Role `healthcheck`

Doel: niet alleen aannemen dat de CT draait, maar controleren dat er effectief HTTP antwoordt.

### Taken

#### HTTP healthcheck against tenant IP

Gebruikt de Ansible `uri` module op:

```text
http://{{ tenant_ip }}/
```

Voorwaarden:

- statuscode tussen 100 en 499 geldt als respons;
- timeout 5 seconden;
- 60 retries met 3 seconden tussen.

Dat betekent dat een verse container tot ongeveer 3 minuten tijd krijgt om webverkeer te beginnen serveren.

#### Provisioning summary (parsed by worker)

Schrijft:

```text
BRIEHOST_RESULT site_id=<...> vmid=<...> ip=<...> hostname=<...> status=live
```

De Python-worker parseert exact deze regel uit de stdout van de playbook en schrijft daarna `vmid` en `ip_address` naar Supabase.

Dit is dus het contract tussen Ansible en de API.

---

## 11. Delete-playbook

`delete_site.yml` is bewust klein.

### Pre-task

- valideert dat `site_id` aanwezig is.

### Role

- hergebruikt enkel `proxmox_cleanup`.

Praktisch gevolg:

- alle CT's met de gematchte `site_id` in hun description worden gestopt en vernietigd;
- de deleteflow hoeft niet te weten welk VMID ooit aan die site toegekend was.

Dat maakt de teardown robuuster als metadata in de database incompleet is.

---

## 12. Gateway-setup playbook

`setup_gateway.yml` draait op de gateway-CT en gebruikt de role `gateway_setup`.

### Wat die role concreet doet

#### Install base packages

Installeert onder meer:

- `curl`
- `gnupg`
- `apt-transport-https`
- `nftables`

#### Install cloudflared apt signing key

Haalt de GPG-key van Cloudflare op.

#### Add cloudflared apt repo

Plaaatst de apt bron voor `cloudflared`.

#### Apply pending apt updates before installing cloudflared

Forceert handlers zodat de nieuwe repo effectief gekend is.

#### Install cloudflared

Installeert het pakket.

#### Check whether cloudflared systemd unit is already registered

Controleert of `/etc/systemd/system/cloudflared.service` al bestaat.

#### Register cloudflared with the tunnel token

Voert eenmalig `cloudflared service install <token>` uit. Dankzij `creates:` gebeurt dit niet opnieuw bij elke run.

#### Ensure cloudflared is enabled + running

Zorgt dat de tunnelservice draait na reboot.

#### Install Caddy apt signing key / Add Caddy apt repo / Install Caddy

Voegt de officiële Caddy-repository toe en installeert Caddy.

#### Render Caddyfile

Schrijft de statische basisconfig via template en valideert die eerst met `caddy validate --config`.

#### Ensure systemd drop-in dir for caddy

Maakt de drop-in map aan.

#### Configure caddy to start with --resume

Overschrijft de systemd `ExecStart` zodat Caddy dynamische routes uit `autosave.json` herneemt.

#### Ensure caddy is enabled + running

Start en enabelt de service.

#### Render nftables rules

Schrijft de firewallregels die de adminpoort afschermen.

#### Ensure nftables is enabled + running

Zet de firewallregels live en persistent.

---

## 13. PHP-template setup + hardening

`setup_php_template.yml` is geen runtimeplaybook per site, maar een onderhoudsplaybook voor het gouden template waaruit nieuwe tenant-CT's gekloond worden.

### 13.1 Voorbereidende taken

- valideert `template_vmid`;
- start de template-CT als die nog uitstaat;
- wacht enkele seconden op init/systemd.

### 13.2 Basissoftware

De playbook installeert in de template onder andere:

- composer
- `php8.2-bcmath`
- `php8.2-curl`
- `php8.2-zip`
- `php8.2-sqlite3`
- `sqlite3`
- `unzip`
- `nodejs`
- `npm`

Hierdoor kunnen gekloonde tenants later Laravel dependencies en frontend-assets bouwen.

### 13.3 Role `hardening_os`

Deze role voert OS-hardening uit binnen de template-CT:

- mount flags `noexec,nosuid,nodev` op `/var/www/html`;
- persistente fstab-aanpassing;
- veilige PHP tempdirs;
- permissies op webroot herstellen;
- `unattended-upgrades` installeren en configureren;
- `auditd` installeren;
- auditregels voor webroot, config en userbestanden;
- logrotate voor audit- en nginx/PHP-logs;
- sysctl hardening zoals `tcp_syncookies`, `rp_filter`, `dmesg_restrict`.

### 13.4 Role `hardening_php`

Deze role voert applicatie-hardening uit:

- gevaarlijke PHP-functies uitschakelen;
- security-module activeren via `phpenmod`;
- `php.ini` aanscherpen;
- aparte hardened FPM pool schrijven;
- standaardpool verwijderen;
- logmappen en logrotate instellen;
- Nginx security headers toevoegen;
- `php-fpm` en `nginx` syntax testen;
- services herstarten.

Belangrijke nuance: deze role gaat uit van een template met Nginx en PHP-FPM, niet Apache.

---

## 14. Waarom deze Ansible-opbouw belangrijk is

1. Elke provisioningrun is reproduceerbaar.
2. De infrastructuurlogica is leesbaar in versiebeheer in plaats van verstopt in Python-subprocesscalls.
3. Ops kan playbooks handmatig heruitvoeren voor diagnose.
4. De worker hoeft alleen statusovergangen en orchestration te doen.
5. Cleanup en delete steunen op tags in de Proxmox-config, niet op vluchtige processtatus.

---

## 15. Operationele risico's en aandachtspunten

1. De provisioning is correct geserialiseerd in de API-worker, maar menselijke acties in de Proxmox UI kunnen nog steeds lockconflicten veroorzaken. Daarom bestaan de retries in `proxmox_clone`.
2. `deploy-site.sh` probeert veel Laravel-automatisering te doen. Dat is krachtig, maar betekent ook dat atypische apps alsnog waarschuwingen of gedeeltelijke deploys kunnen geven.
3. `artisan migrate` is best-effort. Een site kan dus live komen terwijl DB-afhankelijke features later nog fouten geven.
4. `delete_site.yml` vertrouwt op de `description`-tag. Als iemand die handmatig wijzigt, kan cleanup een CT niet meer terugvinden.
