# Systeemdocumentatie: Proxmox-platform

**Hoofdnode:** dc.team10.tm  
**IP-adres beheer:** 172.17.10.1  
**Rol:** host voor de API-container, gateway-container, tenant-templates en tenant-sites

---

## 1. Rol binnen het Brieblast-platform

De Proxmox-node host niet alleen losse containers, maar vormt de uitvoeringslaag voor de volledige Brieblast provisioningketen.

Belangrijkste functies:

1. bewaart het gouden PHP-template waaruit tenant-CT's worden gekloond;
2. host de API-CT die Ansible-triggers uitstoot;
3. host de gateway-CT die publiek verkeer reverse-proxyt;
4. draait de tenant-CT's zelf op het interne netwerk;
5. biedt de `pct`- en Proxmox API-operaties die door Ansible gebruikt worden.

Voor de gedetailleerde taak-per-taak provisioninglogica hoort deze documentatie samen gelezen te worden met `Brieblast_Ansible_Provisioning.md`.

---

## 2. Netwerkbeeld

Volgens de huidige documentatie en Ansible-defaults zijn vooral deze netwerken relevant:

| Interface | Functie |
| --- | --- |
| `vmbr0` | beheer/WAN-zijde van de node |
| `vmbr1` | tenant/LAN-bridge waar gateway en tenant-CT's op zitten |

De provisioning gebruikt in `group_vars/proxmox.yml` expliciet:

```text
tenant_bridge: vmbr1
```

Dat betekent:

- nieuwe tenant-CT's krijgen hun NIC op `vmbr1`;
- hun IPv4-adres komt via DHCP;
- de gateway staat eveneens op dit segment zodat Caddy rechtstreeks naar tenant-IP's kan proxyen.

---

## 3. Relevante containers en VM's

### 3.1 PHP-template

De API verwacht een template-VMID via:

```text
PHP_TEMPLATE_VMID
```

Dat template bevat de software en scripts die elke nieuwe tenant erft, waaronder:

- Nginx
- PHP-FPM
- composer
- Node/NPM
- `/usr/local/bin/deploy-site.sh`

### 3.2 API-container

De API-container runt de FastAPI-service en lanceert Ansible-playbooks.

### 3.3 Gateway-container

De gateway-container draait `cloudflared`, Caddy en nftables, en publiceert tenant-sites.

### 3.4 Tenant-containers

Elke tenantsite krijgt een eigen LXC-container, gekloond uit het template.

Eigenschappen volgens de Ansible-defaults:

- eigen VMID;
- hostname afgeleid van `site_slug` en UUID-fragment;
- DHCP op `vmbr1`;
- `onboot: true`;
- description met `site_id` en `user_id`.

---

## 4. Hoe Proxmox in de provisioningflow gebruikt wordt

De Ansible-pipeline gebruikt zowel de Proxmox API als `pct`-commando's.

### 4.1 Via `community.proxmox.proxmox`

Gebruikt voor:

- full clone van het template;
- resourceconfiguratie;
- netwerkconfig;
- container starten;
- description updaten.

### 4.2 Via `pct`

Gebruikt voor:

- `pct list`
- `pct config`
- `pct stop`
- `pct destroy`
- `pct resize`
- `pct exec`
- `pct push`

Praktisch betekent dit dat Proxmox niet alleen de hypervisorlaag is, maar ook het operationele controlepunt voor deployment en cleanup.

---

## 5. Identificatie en cleanup

Een belangrijke ontwerpkeuze is dat tenant-CT's identificeerbaar zijn via hun `description`.

Voorbeeldvorm:

```text
briehost site=<hostname> ip=<tenant_ip> site_id=<uuid> user_id=<uuid>
```

Die tekst wordt gebruikt door:

- operators in de Proxmox UI;
- de role `proxmox_cleanup` om orphaned of mislukte CT's op te ruimen.

De deleteflow hangt dus niet af van een apart inventarissysteem of van een losse mappingtabel op de host.

---

## 6. Storage en resourceprofiel

Volgens `group_vars/proxmox.yml` worden nieuwe tenants standaard zo aangemaakt:

| Instelling | Waarde |
| --- | --- |
| pool | `briehost` |
| storage | `local-lvm` |
| disk | `8 GB` |
| CPU | `1 core` |
| RAM | `512 MB` |
| swap | `256 MB` |

Omdat `clone_type: full` gebruikt wordt, krijgt elke tenant een eigen volledige clone van het template, in plaats van een linked clone.

---

## 7. Operationele aandachtspunten

1. De worker serializeert jobs, maar een beheerder die tegelijk handmatig clones of wijzigingen doet in de Proxmox UI kan nog lockconflicten veroorzaken.
2. Cleanup werkt op basis van `description`. Handmatige edits aan die description kunnen delete- en recoveryflows verstoren.
3. Tenant-IP's zijn DHCP-gebaseerd. De provisioningflow leest het lease achteraf uit en werkt dan de metadata bij.
4. De gateway vertrouwt op correcte `ip_address` metadata om naar de juiste tenant te proxyen.

---

## 8. Samenhang met andere documenten

- `Brieblast_API.md` beschrijft wanneer provisioning en teardown gestart worden.
- `Brieblast_Gateway.md` beschrijft hoe de gateway verkeer naar tenant-IP's stuurt.
- `Brieblast_Ansible_Provisioning.md` beschrijft exact welke Ansible-taken op Proxmox uitgevoerd worden.
