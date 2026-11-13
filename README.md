# vps-deployer

Kleiner Deploy-Dienst auf dem VPS. Openclaw ruft ihn nach deiner Freigabe im Chat auf. Er startet ein Image aus `ghcr.io/tk-05/…` als Container, sorgt dafür, dass `<name>.tk05.org` per DNS erreichbar ist, und legt einen NPM-Proxy-Host mit dem Wildcard-Zertifikat. Danach prüft er, ob die Seite antwortet. Wenn etwas schiefgeht, nimmt er die Änderungen zurück.

**DNS:** Ohne `CF_API_TOKEN` verlässt sich der Deployer auf den Wildcard-Eintrag `*.tk05.org` und prüft nur, ob der Name auflöst. Ist ein Token gesetzt, legt er pro App eigene A/AAAA-Einträge über die Cloudflare-API an.

Der Dienst ist nur über Tailscale erreichbar: `http://100.84.59.4:8787`. Jede Anfrage braucht den Header `Authorization: Bearer <DEPLOYER_API_TOKEN>`.

## API

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/deployments` | `{project, image, subdomain?, port?, env?, volume_path?}`. Prüft die Eingaben und legt ein Deployment mit Status `pending` an. Das Image wird nur geladen, sonst ändert sich nichts. |
| POST | `/deployments/{id}/approve` | Führt das Deployment aus (asynchron). |
| POST | `/deployments/{id}/cancel` | Verwirft ein `pending`-Deployment. |
| GET | `/deployments/{id}` | Status (`pending, running, succeeded, failed, rolled_back, expired`), Log und URL. |
| GET | `/apps` | Listet die verwalteten Apps. |
| DELETE | `/apps/{name}?remove_volume=false` | Legt ein `pending`-Remove an. Ausgeführt wird es erst nach `approve`. |

## Regeln

- Das Image muss mit `ghcr.io/tk-05/` beginnen und per Digest festgelegt sein (`@sha256:…`).
- Die Subdomain besteht aus `a-z0-9-`, höchstens 40 Zeichen. Reservierte Namen und fremde NPM-Hosts oder DNS-Einträge werden abgelehnt.
- Der Port wird so bestimmt: Angabe in der Anfrage, sonst das Image-Label `de.tk05.port`, sonst ein einzelnes `EXPOSE`.
- Container laufen ohne Host-Ports und Bind-Mounts, mit `no-new-privileges` und mit Speicher- und CPU-Limit. Sie hängen nur im Netzwerk `apps`.
- Geheime Umgebungsvariablen trägst du von Hand in `/opt/vps-deployer/apps/<name>/secrets.env` ein. Über die API kommen nur nicht geheime Variablen.
- Bevor der Dienst etwas in NPM ändert, prüft er, ob alle Zertifikate, auf die Proxy Hosts verweisen, wirklich vorhanden sind. So kann sich der Ausfall vom 13.11.2026 nicht wiederholen.

## Betrieb

Der Dienst wird in `/opt/vps-deployer` betrieben (`compose.yml`, `.env`, `data/`, `docker-config/`, `apps/`):

```bash
docker compose up -d --build
```
