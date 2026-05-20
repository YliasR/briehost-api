# Systeemdocumentatie: Brieblast Gateway

**Hostnaam:** brie-gw  
**Type:** Debian LXC-container  
**Rol:** publieke toegangspoort voor tenant-sites via Cloudflare Tunnel en Caddy

---

## 1. Doel

De gateway is de enige component die publiek webverkeer voor tenant-sites afhandelt.

Ze combineert drie functies:

1. Cloudflare Tunnel beëindigt de publieke connectiviteit zonder inkomende poorten op de Proxmox-host te openen.
2. Caddy routeert requests op basis van `Host` naar het juiste tenant-IP.
3. De API pusht dynamische routes naar Caddy zodra een site live gaat of verwijderd wordt.

De gateway bevat dus geen eigen bron van waarheid. De authoritatieve mapping blijft `public.sites` in Supabase.

---

## 2. Verkeersstroom

```text
Bezoeker
  -> Cloudflare edge
  -> cloudflared op brie-gw
  -> Caddy op brie-gw
  -> tenant-CT op privé-IP:80
```

Concreet voor een werkende tenant:

1. gebruiker bezoekt `https://demo.briehosting.be/`;
2. Cloudflare matcht de wildcard/tunnelconfig;
3. `cloudflared` levert de request lokaal af aan Caddy;
4. Caddy vindt route `site:demo`;
5. Caddy proxyt naar bijvoorbeeld `192.168.10.54:80`.

Als er geen route bestaat, valt de request terug op de catch-all route en krijgt de client een 404.

---

## 3. Caddy als dynamische reverse proxy

### 3.1 Route-ID's

Elke tenantroute krijgt een stabiel id:

```text
site:<subdomain>
```

Voorbeeld:

```text
site:demo
```

Dat id wordt gebruikt om routes idempotent te verwijderen en opnieuw toe te voegen.

### 3.2 Routeopbouw

Een route die door de API wordt gepusht heeft conceptueel deze vorm:

```json
{
  "@id": "site:demo",
  "match": [{"host": ["demo.briehosting.be"]}],
  "handle": [{
    "handler": "reverse_proxy",
    "upstreams": [{"dial": "192.168.10.54:80"}]
  }],
  "terminal": true
}
```

### 3.3 Waarom altijd index `0`

De API zet nieuwe routes expliciet op `/routes/0`.

Reden:

- Caddy evalueert routes in array-volgorde;
- de statische catch-all mag nooit boven een tenantroute staan;
- door vooraan in te voegen blijven de tenantroutes altijd voor de fallback staan.

---

## 4. Hoe de API de gateway aanstuurt

`app/gateway.py` bevat drie relevante acties:

| Functie | Effect |
| --- | --- |
| `register_route()` | voegt of vervangt één route |
| `replace_all_routes()` | wist alle `site:*` routes en bouwt ze opnieuw op |
| `unregister_route()` | verwijdert een route bij teardown |

### 4.1 `register_route()`

Wordt gebruikt na een succesvolle provisioning.

HTTP-sequentie:

1. `DELETE /id/site:<subdomain>`
2. `PUT /config/apps/http/servers/srv0/routes/0`

De delete is bewust tolerant voor `404`: als de route nog niet bestaat is dat geen fout.

### 4.2 `replace_all_routes()`

Wordt gebruikt door `app.sync_gateway.resync()`.

Flow:

1. `GET /config/apps/http/servers/srv0/routes`
2. alle bestaande routes met `@id` dat begint met `site:` worden verzameld;
3. die routes worden één voor één verwijderd;
4. de gewenste routes uit Supabase worden opnieuw ingestoken.

Niet-dynamische routes blijven zo onaangeroerd.

### 4.3 `unregister_route()`

Wordt gebruikt in teardown-jobs. Het is best-effort en idempotent. Een onbekende route is geen probleem.

---

## 5. Persistente en niet-persistente toestand

### 5.1 Waar de bron van waarheid zit

De echte mapping zit in Supabase:

- `sites.subdomain`
- `sites.ip_address`
- alleen rijen met `status='live'` zijn relevant

### 5.2 Wat Caddy bewaart

Caddy bewaart de dynamische routes in zijn live-configuratie en schrijft via `autosave.json` een snapshot weg.

De Ansible-role `gateway_setup` forceert Caddy om met `--resume` te starten. Daardoor worden de dynamische routes bij een gewone Caddy-restart opnieuw geladen als `autosave.json` aanwezig is.

Dat is beter dan de vroegere situatie waarin elke reboot alle dynamische routes verloor, maar Supabase blijft nog altijd de feitelijke waarheid.

---

## 6. Gateway-setup via Ansible

De playbook `infra/ansible/playbooks/setup_gateway.yml` draait op hostgroep `gateway` en gebruikt de role `gateway_setup`.

Die role doet in grote lijnen:

1. basispakketten installeren;
2. Cloudflare apt key en repository toevoegen;
3. `cloudflared` installeren;
4. `cloudflared service install <token>` eenmalig uitvoeren;
5. Cloudsmith-repository voor Caddy toevoegen;
6. Caddy installeren;
7. Caddyfile renderen uit template;
8. systemd drop-in plaatsen zodat Caddy met `--resume` draait;
9. nftables-config renderen;
10. services `cloudflared`, `caddy` en `nftables` enablen en starten.

De gebruikte groepsvariabelen staan in `infra/ansible/inventory/group_vars/gateway.yml`.

Belangrijkste waarden:

- `gateway_admin_bind_ip = 192.168.10.208`
- `gateway_admin_allowed_source = 192.168.10.136`
- `gateway_domain = briehosting.be`
- `cloudflared_tunnel_token` uit environment

---

## 7. Beveiliging van de admin API

De Caddy admin API heeft standaard geen ingebouwde authenticatie. Daarom zijn er twee verdedigingslagen.

### 7.1 Binding op het juiste IP

De admin API wordt niet breed op `0.0.0.0` gezet, maar op het gateway-IP dat bedoeld is voor intern beheer.

### 7.2 nftables

De rendered nftables-regels laten enkel verkeer naar de adminpoort toe vanaf de API-container.

Praktisch gevolg:

- de API-container mag routes pushen;
- tenant-containers op dezelfde bridge mogen dat niet;
- publiek verkeer komt sowieso via Cloudflare Tunnel en niet via de adminpoort binnen.

---

## 8. Failure modes

| Scenario | Gevolg |
| --- | --- |
| `cloudflared` down | publieke requests bereiken Caddy niet |
| Caddy down | tunnel is actief, maar reverse proxy werkt niet |
| route ontbreekt | 404 via catch-all |
| tenant-IP niet bereikbaar | Caddy geeft upstream-fout (typisch 502) |
| admin API niet bereikbaar vanaf de API | provisioning kan slagen maar site is niet publiek |
| gateway reboot | routes worden meestal via `--resume` hersteld; zo niet kan de API resync doen |

---

## 9. Operationele handelingen

### 9.1 Resync forceren

Als Supabase correct is maar de gateway-routes niet, kan de API de volledige set opnieuw pushen met:

```bash
python -m app.sync_gateway
```

### 9.2 Wat controleren bij routingproblemen

1. Is de site in Supabase `live`?
2. Heeft de rij zowel `subdomain` als `ip_address`?
3. Draait Caddy?
4. Draait `cloudflared`?
5. Kan de API-container `http://<gateway-ip>:2019/...` bereiken?
6. Reageert de tenant op `http://<tenant-ip>/`?

---

## 10. Relatie met andere documentatie

- `Brieblast_API.md` beschrijft wanneer routes gepusht en verwijderd worden.
- `Brieblast_Ansible_Provisioning.md` beschrijft hoe de gateway-setup playbook werkt.
- Het Proxmox-document beschrijft de onderliggende host en netwerkbridges waar de gatewaycontainer op draait.
