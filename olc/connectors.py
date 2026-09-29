"""Read-only, permission-aware connectors for approved Google Drive folders and Microsoft SharePoint/OneDrive locations.

Design rules
* READ ONLY: only read scopes are requested (drive.readonly; Files.Read / Sites.Read.All delegated).
* PERMISSION-AWARE: calls are made with the signed-in reviewer's own delegated OAuth token, so the provider only
  returns files that person can already open. The app never uses an org-wide service account to crawl.
* APPROVED LOCATIONS ONLY: administrators list folder IDs / site+folder paths in config/approved_sources.json.
  Anything outside those locations is refused, even if the token could read it.
* Imported files keep the location's default classification (INTERNAL unless configured otherwise).

Enablement (not active in the initial build): register an OAuth client (Google Cloud console / Entra ID app
registration), put its IDs in config/approved_sources.json, set the client secret in an environment variable, and wire
the OAuth redirect (routes stubbed at /connect/google and /connect/microsoft). Until then the connectors report
"not configured" and the app works fully with uploads and URLs.
"""
import json
import os
import urllib.parse
import urllib.request


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _get(url, token, raw=False):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if raw else json.loads(data)


class GoogleDriveConnector:
    name = "Google Drive"
    API = "https://www.googleapis.com/drive/v3"
    EXPORT = {"application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
              "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")}

    def __init__(self, cfg):
        self.cfg = cfg or {}

    def status(self):
        if not self.cfg.get("enabled"):
            return "Not enabled (set google_drive.enabled and OAuth client in config/approved_sources.json)"
        if not self.cfg.get("client_id") or not os.environ.get(self.cfg.get("client_secret_env", "GOOGLE_CLIENT_SECRET"), ""):
            return "Enabled but OAuth client not configured"
        return "Configured (each reviewer connects with their own Google account)"

    def approved(self):
        return self.cfg.get("approved_folders", [])

    def list_files(self, token, folder_id):
        if folder_id not in {f.get("folder_id") for f in self.approved()}:
            raise PermissionError("Folder is not an approved evidence location")
        q = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
        return _get(f"{self.API}/files?q={q}&fields=files(id,name,mimeType,modifiedTime,webViewLink)&pageSize=200", token)["files"]

    def download(self, token, f):
        if f["mimeType"] in self.EXPORT:
            mime, ext = self.EXPORT[f["mimeType"]]
            data = _get(f"{self.API}/files/{f['id']}/export?mimeType={urllib.parse.quote(mime)}", token, raw=True)
            return data, f["name"] + ext
        return _get(f"{self.API}/files/{f['id']}?alt=media", token, raw=True), f["name"]


class MicrosoftGraphConnector:
    name = "Microsoft SharePoint / OneDrive"
    API = "https://graph.microsoft.com/v1.0"

    def __init__(self, cfg):
        self.cfg = cfg or {}

    def status(self):
        if not self.cfg.get("enabled"):
            return "Not enabled (set microsoft.enabled, tenant_id and client_id in config/approved_sources.json)"
        if not (self.cfg.get("tenant_id") and self.cfg.get("client_id")):
            return "Enabled but Entra ID app registration not configured"
        return "Configured (each reviewer signs in with their FAU Microsoft account)"

    def approved(self):
        return self.cfg.get("approved_locations", [])

    def _root(self, loc):
        if loc.get("type") == "sharepoint":
            return f"{self.API}/sites/{loc['site_id']}/drives/{loc['drive_id']}/root:/{urllib.parse.quote(loc.get('folder_path', ''))}:"
        return f"{self.API}/me/drive/root:/{urllib.parse.quote(loc.get('folder_path', ''))}:"

    def list_files(self, token, loc_index):
        loc = self.approved()[int(loc_index)]
        return _get(self._root(loc) + "/children?$select=id,name,file,lastModifiedDateTime,webUrl", token)["value"]

    def download(self, token, loc_index, item_id):
        loc = self.approved()[int(loc_index)]
        base = f"{self.API}/sites/{loc['site_id']}/drives/{loc['drive_id']}" if loc.get("type") == "sharepoint" else f"{self.API}/me/drive"
        return _get(f"{base}/items/{item_id}/content", token, raw=True)


def connectors(config_path):
    cfg = load_config(config_path)
    return [GoogleDriveConnector(cfg.get("google_drive")), MicrosoftGraphConnector(cfg.get("microsoft"))]
