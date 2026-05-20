# Systeemdocumentatie: Brieblast API

**Hostnaam:** brieapi  
**Type:** FastAPI-service in een Debian LXC-container  
**Rol:** controlelaag voor uploads, provisioning, teardown, gateway-sync, healthchecks en betalingen

---

## 1. Doel

`briehost-api` is de server-side laag tussen de frontend (`brieblast-landing`), Supabase, Proxmox en de gateway.

De service doet vandaag meer dan alleen ZIP-uploads ontvangen:

1. authenticatie van dashboardgebruikers via Supabase JWT;
2. uploaden van een ZIP of importeren van een publieke Git-repository;
3. afdwingen van planlimieten zoals aantal sites en opslagverbruik;
4. queueing van provisioning- en teardown-jobs;
5. malware- en ZIP-policycontroles voor er iets naar een tenant-container gaat;
6. uitvoeren van Ansible-playbooks voor provisioning en verwijdering;
7. registreren van gateway-routes voor publieke bereikbaarheid;
8. periodieke healthchecks voor reeds live sites;
9. aanmaken en opvolgen van payment intents.

De frontend praat dus niet rechtstreeks met Proxmox of Caddy. Alles loopt via deze API.

---

## 2. Hoofdarchitectuur

```text
Frontend (brieblast-landing)
  -> HTTPS + Bearer JWT
briehost-api (FastAPI)
  -> Supabase (sites, profiles, payment_intents, health RPC)
  -> lokale opslag (ZIP-bestanden onder STORAGE_ROOT)
  -> malware scanners (php-malware-finder + clamd)
  -> Ansible playbooks
  -> Proxmox API / pct op de Proxmox-host
  -> Caddy admin API op de gateway
```

Belangrijke modules in de code:

| Module | Verantwoordelijkheid |
| --- | --- |
| `app/main.py` | FastAPI-app, CORS, startup/shutdown hooks |
| `app/auth.py` | Bearer-token uitlezen en Supabase JWKS-verificatie |
| `app/routes/sites.py` | upload-, repo-import-, provision- en delete-endpoints |
| `app/routes/payments.py` | payment intents, status polling, webhooks |
| `app/worker.py` | FIFO queue, scan-flow, ansible-run, teardown-run |
| `app/storage.py` | veilige ZIP-paden en ZIP-policycontrole |
| `app/repo.py` | publieke Git-clone en verpakken naar ZIP |
| `app/limits.py` | planlimieten controleren |
| `app/gateway.py` | Caddy admin API voor routebeheer |
| `app/sync_gateway.py` | volledige gateway-resync vanuit Supabase |
| `app/health.py` | periodieke runtime-healthchecks van live sites |
| `app/payments/*` | providerlogica voor Stripe, CoinGate, PayPal-stub en skip-stub |

---

## 3. Startup- en runtimegedrag

Bij het starten van de API gebeuren er drie belangrijke dingen:

1. `app/main.py` laadt configuratie en zet CORS op.
2. In de eerste startup-hook probeert `app.sync_gateway.resync()` alle live routes opnieuw in Caddy te pushen.
3. In de tweede startup-hook start de service:
   - de FIFO consumer voor provision- en teardown-jobs;
   - de periodieke site-health-monitor.

Bij shutdown worden de health-monitor en de consumer netjes gestopt.

### 3.1 Health endpoint

`GET /healthz` geeft altijd enkel dit terug:

```json
{ "status": "ok" }
```

Dat endpoint zegt alleen dat de API-process zelf antwoordt. Het zegt niets over Supabase, Proxmox, de gateway of de workerqueue.

---

## 4. Authenticatie

Alle beschermde endpoints gebruiken `app.auth.current_user_id`.

De flow is:

1. De API leest de `Authorization`-header.
2. Alleen `Bearer <token>` is geldig.
3. De public keys worden opgehaald via `SUPABASE_URL/auth/v1/.well-known/jwks.json`.
4. Het JWT wordt gevalideerd tegen `RS256` of `ES256`.
5. De `aud` claim moet overeenkomen met `SUPABASE_JWT_AUDIENCE` (standaard `authenticated`).
6. De `sub` claim wordt gebruikt als `user_id`.

Als er iets fout loopt, krijgt de client `401 Unauthorized`.

Opmerking: de code ondersteunt momenteel geen HS256-fallback. De setup verwacht dus een Supabase-project met JWKS-compatibele signing keys.

---

## 5. Datastromen rond sites

De centrale tabel is `public.sites` in Supabase. Deze tabel houdt per site onder meer bij:

- `id`
- `user_id`
- `name`
- `original_filename`
- `size_bytes`
- `status`
- `error_message`
- `ip_address`
- `vmid`
- `subdomain`

De API gebruikt de service-role key server-side via `app.db.admin_client()`. Daardoor omzeilt ze RLS voor writes, terwijl gewone gebruikers via Supabase enkel hun eigen records lezen.

### 5.1 Statusmodel

De worker gebruikt deze statuswaarden:

```text
uploaded -> queued -> scanning -> provisioning -> live
                             \-> scan_failed
                provisioning \-> failed
```

Betekenis:

| Status | Betekenis |
| --- | --- |
| `uploaded` | bestand of repo is opgeslagen, nog niet ingepland |
| `queued` | job zit in de FIFO-wachtrij |
| `scanning` | ZIP-policy en malwarescans lopen |
| `scan_failed` | ZIP of malwarecontrole is afgekeurd |
| `provisioning` | Ansible-playbook draait |
| `live` | provisioning is gelukt |
| `failed` | provisioning of workerflow is mislukt |

---

## 6. Site-endpoints

De routes leven onder `prefix="/api/sites"`.

### 6.1 `POST /api/sites/upload`

Doel: een ZIP-bestand opslaan, nog zonder provisioning te starten.

Input:

- `multipart/form-data`
- veldnaam: `file`
- bestand moet eindigen op `.zip`
- auth vereist

Flow:

1. controle op extensie `.zip`;
2. planlimiet `max_sites` wordt eerst gecontroleerd;
3. bestand wordt in chunks van 1 MB naar disk geschreven;
4. bij overschrijding van `MAX_UPLOAD_BYTES` stopt de upload met `413`;
5. daarna wordt opslaglimiet gecontroleerd;
6. er wordt een rij in `sites` toegevoegd met status `uploaded`;
7. de response bevat een voorgesteld subdomein.

Opslagpad op disk:

```text
STORAGE_ROOT/<user_id>/<slug>-<site_id>.zip
```

Responsevorm:

```json
{
  "siteId": "uuid",
  "status": "uploaded",
  "suggestedSubdomain": "mijn-site"
}
```

### 6.2 `POST /api/sites/upload-repo`

Doel: een publieke repository binnenhalen en omzetten naar exact dezelfde ZIP-pipeline als een normale upload.

Input-body:

```json
{
  "repoUrl": "https://github.com/owner/repo",
  "branch": "main"
}
```

Belangrijk gedrag:

- alleen `https://` URLs;
- geen credentials in de URL;
- alleen hosts uit de allowlist: `github.com`, `gitlab.com`, `git.gay`;
- branchnaam wordt streng gevalideerd;
- clone gebeurt shallow, zonder tags;
- `.git` wordt verwijderd voor het zippen;
- symlinks in de repo zijn niet toegestaan;
- `max_files` en `max_bytes` worden al tijdens repo-import afgedwongen.

De response is hetzelfde model als bij `/upload`.

### 6.3 `POST /api/sites/{site_id}/provision`

Doel: een eerder geüploade of geïmporteerde site echt in de provisioningqueue zetten.

Input-body:

```json
{
  "subdomain": "gekozen-naam"
}
```

Flow:

1. subdomein wordt via `derive_subdomain()` genormaliseerd;
2. de site moet eigendom zijn van de gebruiker;
3. de site moet status `uploaded` hebben;
4. de queue-diepte mag niet hoger zijn dan `MAX_CONCURRENT_PROVISIONS`;
5. het subdomein wordt globaal op uniciteit gecontroleerd;
6. het subdomein wordt vooraf op de rij geclaimd;
7. de ZIP-locatie wordt gereconstrueerd;
8. `enqueue_provision()` zet de job in de queue en zet status `queued`.

Response:

```json
{
  "siteId": "uuid",
  "status": "queued",
  "subdomain": "gekozen-naam"
}
```

### 6.4 `DELETE /api/sites/{site_id}`

Doel: een site verwijderen.

Belangrijk ontwerpbesluit: de API verwijdert eerst de `sites`-rij uit Supabase en plant daarna een teardown-job in. Daardoor ziet de frontend meteen dat de site weg is, ook al moet Proxmox nog opruimen.

Teardown-job doet daarna best-effort:

1. gateway-route verwijderen;
2. ZIP op disk verwijderen;
3. delete-playbook draaien om de tenant-CT te stoppen en te vernietigen.

Response:

```json
{
  "siteId": "uuid",
  "status": "deleting"
}
```

---

## 7. Workerqueue en provisioningflow

`app/worker.py` gebruikt geen externe queue zoals Celery of Redis. Er draait intern één `asyncio.Queue` met exact één consumer.

### 7.1 Waarom één consumer

De queue is bewust FIFO en single-consumer:

- Proxmox-clones mogen niet onderling racen;
- teardown en provision delen dezelfde queue;
- de UI kan duidelijk `queued` tonen in plaats van meerdere jobs tegelijk half te starten.

`queue_depth()` telt zowel de lopende job als de wachtende jobs mee. Die waarde wordt gebruikt voor backpressure op `/provision`.

### 7.2 Provisioningflow in detail

Voor één provision-job gebeurt dit:

1. status wordt `scanning`;
2. `validate_zip_policy()` controleert:
   - max aantal files;
   - max uitgepakte grootte;
   - max compressieratio;
   - geen symlinks;
   - geen path traversal;
3. indien `ENABLE_MALWARE_SCAN=true`:
   - php-malware-finder / YARA-regels;
   - daarna `clamd` stream-scan;
4. bij fout wordt `scan_failed` gezet;
5. anders wordt status `provisioning`;
6. de worker roept `ansible-playbook` aan met JSON extra vars;
7. bij `rc == 0` wordt eerst status `live` gezet;
8. daarna worden `ip_address` en `vmid` best-effort weggeschreven;
9. een eerste healthcheck `up` wordt geregistreerd;
10. tenslotte wordt de gateway-route aangemaakt.

Dat `live` eerst wordt gezet is bewust. Als het nà de provisioning fout loopt bij een metadata-update of gateway-call, mag een perfect draaiende tenant niet terug op `failed` terechtkomen.

### 7.3 Teardownflow

Een teardown-job doet:

1. `unregister_route()` als er een subdomein was;
2. lokale ZIP verwijderen als pad gekend is;
3. `delete_site.yml` draaien, wat intern `proxmox_cleanup` hergebruikt.

Fouten in teardown worden alleen gelogd. Er bestaat dan geen `sites`-rij meer om een foutstatus op te bewaren.

---

## 8. Gatewayintegratie

Bij een succesvolle provisioning gebruikt de worker `app.gateway.register_route(settings, subdomain, ip)`.

Dat doet aan de Caddy admin API:

1. `DELETE /id/site:<subdomain>` om een oude route te verwijderen;
2. `PUT /config/apps/http/servers/srv0/routes/0` om de nieuwe route vooraan in de routes-array te zetten.

`app.sync_gateway.resync()` loopt ook bij API-startup en kan manueel aangeroepen worden met:

```bash
python -m app.sync_gateway
```

Die resync leest alle `status='live'` sites met `subdomain` en `ip_address` en pusht de volledige set opnieuw.

---

## 9. Runtime health monitoring

Naast de Ansible-healthcheck direct na deploy bestaat er ook een periodieke monitor in `app/health.py`.

Die monitor:

1. leest alle live sites uit Supabase;
2. bouwt een URL op:
   - publiek via `https://<subdomain>.<gateway_domain>/` als subdomein beschikbaar is;
   - anders fallback naar `http://<ip_address>/`;
3. voert parallelle HTTP-checks uit;
4. roept de RPC `record_site_health_check(...)` aan.

Configuratie:

- `HEALTHCHECK_ENABLED`
- `HEALTHCHECK_INTERVAL_SECONDS`
- `HEALTHCHECK_TIMEOUT_SECONDS`
- `HEALTHCHECK_MAX_CONCURRENCY`
- `HEALTHCHECK_PUBLIC_SCHEME`

De statuspagina van het platform hoort dus niet alleen te kijken naar `sites.status = live`, maar ook naar recente healthchecks.

---

## 10. Payments API

De paymentroutes leven onder `prefix="/api/payments"`.

### 10.1 Wat effectief gebouwd is

Huidige staat in code:

| Provider | Status |
| --- | --- |
| Stripe | geïmplementeerd |
| CoinGate | geïmplementeerd |
| PayPal | stub, nog niet afgewerkt |
| Skip | stub, nog niet afgewerkt |

### 10.2 Endpoints

| Endpoint | Doel |
| --- | --- |
| `POST /api/payments/intents` | checkout aanmaken voor een plan + provider |
| `GET /api/payments/intents` | eigen payment intents oplijsten |
| `GET /api/payments/intents/{intent_id}` | detail/status van één intent |
| `POST /api/payments/skip` | gepland skip-pad, maar provider is nog niet af |
| `POST /api/payments/stripe/webhook` | Stripe-webhooks |
| `POST /api/payments/paypal/webhook` | voorzien, maar nog niet af |
| `POST /api/payments/coingate/webhook` | CoinGate-callbacks |

### 10.3 Create intent

`POST /api/payments/intents` verwacht:

```json
{
  "planId": "smol_brie",
  "provider": "stripe"
}
```

De route:

1. valideert `planId` en provider;
2. vraagt de providerspecifieke `create_intent()` op;
3. schrijft een rij in `payment_intents`;
4. retourneert `intentId`, `checkoutUrl` en `provider`.

### 10.4 Stripe-flow

Stripe gebruikt hosted Checkout Sessions.

Succesflow:

1. API maakt een Checkout Session aan;
2. metadata bevat `intent_id`, `user_id` en `plan_id`;
3. frontend stuurt gebruiker naar `checkoutUrl`;
4. Stripe roept `/api/payments/stripe/webhook` aan;
5. webhookverificatie gebruikt `STRIPE_WEBHOOK_SECRET`;
6. bij `checkout.session.completed` met `payment_status=paid` wordt de intent `succeeded`;
7. de route zet daarna `profiles.plan` om.

Async methods zoals Bancontact worden mee ondersteund door de eventmatrix:

- `checkout.session.completed` met `unpaid` -> nog `pending`;
- `checkout.session.async_payment_succeeded` -> `succeeded`;
- `checkout.session.async_payment_failed` -> `failed`.

### 10.5 CoinGate-flow

CoinGate gebruikt een hosted crypto-checkout.

Belangrijk:

- order wordt aangemaakt via `COINGATE_API_BASE`;
- callback body is form-urlencoded, niet JSON;
- verificatie gebeurt niet via hun signature-header maar via een round-tripped token op basis van `HMAC(intent_id, COINGATE_WEBHOOK_SECRET)`;
- bij success wordt ook bedrag en valuta nog vergeleken met de originele `payment_intents`-rij.

### 10.6 Payment storage

`payment_intents` bewaart onder andere:

- `user_id`
- `plan_id`
- `provider`
- `provider_ref`
- `amount_cents`
- `currency`
- `status`

De unieke sleutel is `(provider, provider_ref)` zodat retries van webhooks dezelfde rij bijwerken in plaats van duplicaten te maken.

---

## 11. Planlimieten

Planlimieten staan in `app/plans.py`.

Huidige API-kant:

| Plan | Max sites | Max storage |
| --- | --- | --- |
| `none` | 0 | 0 B |
| `smol_brie` | 1 | 5 GB |
| `thicc_brie` | onbeperkt | 50 GB |
| `mega_brie` | onbeperkt | 200 GB |
| `admin` | onbeperkt | onbeperkt |

`app/limits.py` dwingt:

1. site-count limiet af vóór een grote upload echt wordt ingestreamd;
2. opslaglimiet af nadat de echte bestandsgrootte gekend is.

Bij overschrijding krijgt de gebruiker `402 Payment Required`, met een machineleesbare foutstructuur voor de frontend.

---

## 12. Belangrijkste configuratievariabelen

### 12.1 Core

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_JWT_AUDIENCE`
- `API_HOST`
- `API_PORT`
- `ALLOWED_ORIGINS`

### 12.2 Provisioning

- `PROVISIONER_BACKEND`
- `ANSIBLE_PLAYBOOK_PATH`
- `ANSIBLE_DELETE_PLAYBOOK_PATH`
- `ANSIBLE_INVENTORY_PATH`
- `ANSIBLE_EXTRA_VARS_JSON`
- `ANSIBLE_TIMEOUT_SECONDS`
- `MAX_CONCURRENT_PROVISIONS`

### 12.3 Uploads en scans

- `STORAGE_ROOT`
- `MAX_UPLOAD_BYTES`
- `MAX_ZIP_FILES`
- `MAX_ZIP_UNCOMPRESSED_BYTES`
- `MAX_ZIP_COMPRESSION_RATIO`
- `ENABLE_MALWARE_SCAN`
- `CLAMD_SOCKET`
- `CLAMD_HOST`
- `CLAMD_PORT`

### 12.4 Gateway en publiek verkeer

- `GATEWAY_CADDY_ADMIN_URL`
- `GATEWAY_DOMAIN`
- `GATEWAY_REQUEST_TIMEOUT_SECONDS`

### 12.5 Runtime monitoring

- `HEALTHCHECK_ENABLED`
- `HEALTHCHECK_INTERVAL_SECONDS`
- `HEALTHCHECK_TIMEOUT_SECONDS`
- `HEALTHCHECK_MAX_CONCURRENCY`
- `HEALTHCHECK_PUBLIC_SCHEME`

### 12.6 Payments

- `PAYMENTS_RETURN_BASE_URL`
- `PAYMENTS_API_BASE_URL`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `PAYPAL_*`
- `COINGATE_API_KEY`
- `COINGATE_API_BASE`
- `COINGATE_WEBHOOK_SECRET`

---

## 13. Operationele aandachtspunten

1. De interne queue is procesgeheugen. Een API-restart tijdens een lopende provision-job verliest die job.
2. Een site kan `live` zijn terwijl de gateway-push mislukt is. Dan werkt de tenant via privé-IP vaak al, maar nog niet publiek.
3. De paymentroutes zijn gedeeltelijk afgewerkt. Stripe en CoinGate zijn bruikbaar, PayPal en skip nog niet.
4. De startup-resync van de gateway is best-effort. Als de gateway offline is bij API-start, blijft de API wel gewoon opkomen.
5. De deleteflow verwijdert de DB-rij voor de infrastructuur gegarandeerd opgeruimd is. Dat is bewust gekozen voor snelle UI-feedback, maar betekent dat ops soms orphaned CT's moet opruimen als teardown fout loopt.
