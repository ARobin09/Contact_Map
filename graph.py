import os
import json
import threading
import webbrowser
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/contacts"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

app = Flask(__name__)
CORS(app)
service = None


# ─── Auth ────────────────────────────────────────────────────────────────────

def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


# ─── Data fetching ────────────────────────────────────────────────────────────

PERSON_FIELDS = ",".join([
    "names", "emailAddresses", "phoneNumbers", "birthdays",
    "addresses", "organizations", "relations", "urls", "biographies",
    "nicknames", "imClients", "events", "memberships", "genders",
    "externalIds", "userDefined",
])

# Built-in Google system groups we never want to show
def fetch_groups():
    groups = {}
    result = service.contactGroups().list().execute()
    for g in result.get("contactGroups", []):
        if g.get("groupType") != "USER_CONTACT_GROUP":
            continue
        groups[g["resourceName"]] = {
            "name": g.get("name", ""),
            "memberCount": g.get("memberCount", 0),
        }
    return groups

def parse_contact(person, groups):
    def first(lst, *keys):
        if not lst: return None
        val = lst[0]
        for k in keys:
            val = val.get(k) if isinstance(val, dict) else None
        return val

    names      = person.get("names", [])
    name       = first(names, "displayName") or "Unnamed"
    emails     = [e["value"] for e in person.get("emailAddresses", []) if e.get("value")]
    phones     = [{"number": p["value"], "type": p.get("type","other")} for p in person.get("phoneNumbers",[]) if p.get("value")]
    bday_raw   = person.get("birthdays",[{}])[0].get("date") if person.get("birthdays") else None
    birthday   = f"{bday_raw.get('year','????')}-{bday_raw.get('month','??'):02d}-{bday_raw.get('day','??'):02d}" if bday_raw and isinstance(bday_raw.get('month'), int) else None
    addresses  = [{"street": a.get("streetAddress",""), "city": a.get("city",""), "region": a.get("region",""), "country": a.get("country",""), "postalCode": a.get("postalCode",""), "type": a.get("type","home"), "formatted": a.get("formattedValue","")} for a in person.get("addresses",[])]
    orgs       = [{"name": o.get("name"), "title": o.get("title"), "department": o.get("department")} for o in person.get("organizations",[])]
    relations  = [{"name": r.get("person",""), "type": r.get("type", r.get("formattedType","other"))} for r in person.get("relations",[])]
    urls       = [{"url": u.get("value"), "type": u.get("type","other")} for u in person.get("urls",[])]
    bio        = first(person.get("biographies",[]), "value")
    nicknames  = [n["value"] for n in person.get("nicknames",[]) if n.get("value")]
    gender     = first(person.get("genders",[]), "value")
    events     = [{"type": e.get("type", e.get("formattedType","other")), "date": e.get("date")} for e in person.get("events",[])]
    im_clients = [{"username": i.get("username"), "protocol": i.get("protocol", i.get("formattedProtocol",""))} for i in person.get("imClients",[])]
    custom     = [{"key": u.get("key"), "value": u.get("value")} for u in person.get("userDefined",[])]
    group_rns  = [m["contactGroupMembership"]["contactGroupResourceName"] for m in person.get("memberships",[]) if "contactGroupMembership" in m]
    group_names= [groups[g]["name"] if g in groups else g for g in group_rns]

    return {
        "resourceName": person.get("resourceName"),
        "etag": person.get("etag",""),
        "name": name, "emails": emails, "phones": phones,
        "birthday": birthday, "gender": gender,
        "addresses": addresses, "organizations": orgs,
        "relations": relations, "urls": urls, "bio": bio,
        "nicknames": nicknames, "events": events,
        "imClients": im_clients, "customFields": custom,
        "groups": group_rns, "groupNames": group_names,
    }

def fetch_contacts(groups):
    contacts, token = [], None
    while True:
        result = service.people().connections().list(
            resourceName="people/me", pageSize=1000,
            personFields=PERSON_FIELDS, pageToken=token,
        ).execute()
        for p in result.get("connections", []):
            contacts.append(parse_contact(p, groups))
        token = result.get("nextPageToken")
        if not token: break
    return contacts


# ─── API routes ───────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    groups   = fetch_groups()
    contacts = fetch_contacts(groups)
    # Serialize groups for frontend: { resourceName: {name, system} }
    resp = jsonify({"groups": groups, "contacts": contacts})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.json
    rn   = data.get("resourceName")
    if not rn:
        return jsonify({"error": "Missing resourceName"}), 400

    def build_body(etag):
        """Build the update body and field list from the request data."""
        body   = {"etag": etag}
        fields = []

        if "name" in data:
            parts = data["name"].strip().split(" ", 1)
            body["names"] = [{"givenName": parts[0], "familyName": parts[1] if len(parts) > 1 else ""}]
            fields.append("names")

        if "emails" in data:
            body["emailAddresses"] = [{"value": e} for e in data["emails"] if e]
            fields.append("emailAddresses")

        if "phones" in data:
            body["phoneNumbers"] = [{"value": p["number"], "type": p.get("type","other")} for p in data["phones"] if p.get("number")]
            fields.append("phoneNumbers")

        if "bio" in data:
            body["biographies"] = [{"value": data["bio"], "contentType": "TEXT_PLAIN"}]
            fields.append("biographies")

        if "nickname" in data:
            body["nicknames"] = [{"value": data["nickname"]}] if data["nickname"] else []
            fields.append("nicknames")

        if "events" in data:
            processed = []
            for ev in data["events"]:
                date_str = ev.get("date", "")
                parts = date_str.split("-")
                if len(parts) == 3:
                    try:
                        year_str, month_str, day_str = parts
                        month = int(month_str)
                        day   = int(day_str)
                        year  = 0 if year_str.strip("?") == "" else int(year_str)
                        date_obj = {"month": month, "day": day}
                        if year:
                            date_obj["year"] = year
                        processed.append({"type": ev.get("type", "anniversary"), "date": date_obj})
                    except ValueError:
                        pass
            body["events"] = processed
            fields.append("events")

        if "birthday" in data:
            if not data["birthday"]:
                # Clear the birthday
                body["birthdays"] = []
                fields.append("birthdays")
            else:
                parts = data["birthday"].split("-")
                if len(parts) == 3:
                    try:
                        year_str, month_str, day_str = parts
                        month = int(month_str)
                        day   = int(day_str)
                        year  = 0 if year_str.strip("?") == "" else int(year_str)
                        date_obj = {"month": month, "day": day}
                        if year:
                            date_obj["year"] = year
                        body["birthdays"] = [{"date": date_obj}]
                        fields.append("birthdays")
                    except ValueError:
                        pass

        if "addresses" in data:
            body["addresses"] = [
                {
                    "streetAddress": a.get("street", ""),
                    "city": a.get("city", ""),
                    "region": a.get("region", ""),
                    "country": a.get("country", ""),
                    "postalCode": a.get("postalCode", ""),
                    "type": a.get("type", "home"),
                }
                for a in data["addresses"]
            ]
            fields.append("addresses")

        if "relations" in data:
            body["relations"] = [
                {"person": r["name"], "type": r.get("type", "other")}
                for r in data["relations"] if r.get("name")
            ]
            fields.append("relations")

        return body, fields

    # Retry up to 3 times to handle etag race conditions
    last_error = None
    for attempt in range(3):
        try:
            # Always fetch a fresh etag right before the update
            person = service.people().get(resourceName=rn, personFields=PERSON_FIELDS).execute()
            etag   = person.get("etag", "")
            body, fields = build_body(etag)

            if not fields and "groups" not in data:
                return jsonify({"error": "No fields to update"}), 400

            # Handle group membership changes
            if "groups" in data:
                current_groups = set(
                    m["contactGroupMembership"]["contactGroupResourceName"]
                    for m in person.get("memberships", [])
                    if "contactGroupMembership" in m
                )
                new_groups = set(data["groups"])
                group_errors = []
                for grn in new_groups - current_groups:
                    try:
                        service.contactGroups().members().modify(
                            resourceName=grn, body={"resourceNamesToAdd": [rn]}
                        ).execute()
                        print(f"Added to group {grn}")
                    except Exception as e:
                        print(f"Error adding to group {grn}: {e}")
                        group_errors.append(str(e))
                for grn in current_groups - new_groups:
                    try:
                        service.contactGroups().members().modify(
                            resourceName=grn, body={"resourceNamesToRemove": [rn]}
                        ).execute()
                        print(f"Removed from group {grn}")
                    except Exception as e:
                        print(f"Error removing from group {grn}: {e}")
                        group_errors.append(str(e))
                if group_errors:
                    return jsonify({"error": "Group update failed: " + group_errors[0]}), 500

            if fields:
                service.people().updateContact(
                    resourceName=rn,
                    updatePersonFields=",".join(fields),
                    body=body,
                ).execute()

            return jsonify({"success": True})

        except Exception as e:
            last_error = str(e)
            print(f"Update attempt {attempt + 1} failed: {e}")
            if "etag" not in last_error.lower():
                break  # Not an etag issue, don't retry

    return jsonify({"error": f"Update failed: {last_error}"}), 500


@app.route("/api/create_group", methods=["POST"])
def api_create_group():
    data = request.json
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    try:
        result = service.contactGroups().create(
            body={"contactGroup": {"name": name}}
        ).execute()
        return jsonify({
            "success": True,
            "resourceName": result["resourceName"],
            "name": result["name"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/delete_group", methods=["POST"])
def api_delete_group():
    data = request.json
    rn   = data.get("resourceName", "").strip()
    if not rn:
        return jsonify({"error": "resourceName is required"}), 400
    try:
        service.contactGroups().delete(resourceName=rn).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Graph HTML ───────────────────────────────────────────────────────────────

GRAPH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Contact Map</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+CiAgPHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iOCIgZmlsbD0iIzBkMGYxNCIvPgogIDwhLS0gZWRnZXMgLS0+CiAgPGxpbmUgeDE9IjgiIHkxPSI4IiB4Mj0iMjQiIHkyPSIxNiIgc3Ryb2tlPSIjM2Q2ZmQ0IiBzdHJva2Utd2lkdGg9IjEuNSIvPgogIDxsaW5lIHgxPSI4IiB5MT0iOCIgeDI9IjE2IiB5Mj0iMjYiIHN0cm9rZT0iIzNkNmZkNCIgc3Ryb2tlLXdpZHRoPSIxLjUiLz4KICA8bGluZSB4MT0iMjQiIHkxPSIxNiIgeDI9IjE2IiB5Mj0iMjYiIHN0cm9rZT0iIzNkNmZkNCIgc3Ryb2tlLXdpZHRoPSIxLjUiLz4KICA8bGluZSB4MT0iOCIgeTE9IjgiIHgyPSIyNCIgeTI9IjYiIHN0cm9rZT0iIzNkNmZkNCIgc3Ryb2tlLXdpZHRoPSIxLjUiLz4KICA8IS0tIGdyb3VwIG5vZGUgKHBpbmssIGxhcmdlcikgLS0+CiAgPGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iNC41IiBmaWxsPSIjZTg1YjhkIi8+CiAgPCEtLSBwZXJzb24gbm9kZXMgKGJsdWUsIHNtYWxsZXIpIC0tPgogIDxjaXJjbGUgY3g9IjgiIGN5PSI4IiByPSIzIiBmaWxsPSIjNWI4ZGVlIi8+CiAgPGNpcmNsZSBjeD0iMjQiIGN5PSIxNiIgcj0iMyIgZmlsbD0iIzViOGRlZSIvPgogIDxjaXJjbGUgY3g9IjE2IiBjeT0iMjYiIHI9IjMiIGZpbGw9IiM1YjhkZWUiLz4KICA8Y2lyY2xlIGN4PSIyNCIgY3k9IjYiIHI9IjMiIGZpbGw9IiM1YjhkZWUiLz4KPC9zdmc+">
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.css">
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0d0f14;
    --surface: #13161e;
    --surface2: #1a1e28;
    --border: #252a38;
    --accent: #5b8dee;
    --accent2: #e85b8d;
    --text: #e2e8f8;
    --muted: #6b7494;
    --success: #3ecf8e;
    --danger: #e85b5b;
  }

  body { font-family: 'DM Sans', sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

  header { display: flex; align-items: center; gap: 12px; padding: 14px 24px; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
  header h1 { font-family: 'DM Mono', monospace; font-size: 15px; font-weight: 500; letter-spacing: 0.05em; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .legend { display: flex; gap: 16px; margin-left: auto; font-size: 12px; color: var(--muted); font-family: 'DM Mono', monospace; }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }

  .main { display: flex; flex: 1; overflow: hidden; position: relative; }
  #graph { flex: 1; background: var(--bg); width: 100%; }

  /* Panel */
  #panel { width: 360px; background: var(--surface); border-left: 1px solid var(--border); display: flex; flex-direction: column; transform: translateX(100%); transition: transform 0.3s cubic-bezier(0.4,0,0.2,1); flex-shrink: 0; position: absolute; right: 0; top: 0; bottom: 0; z-index: 10; }
  #panel.open { transform: translateX(0); }

  .panel-header { padding: 18px 20px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .panel-header h2 { font-family: 'DM Mono', monospace; font-size: 12px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }
  #close-panel { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 20px; line-height: 1; padding: 2px 6px; border-radius: 4px; }
  #close-panel:hover { color: var(--text); background: var(--surface2); }
  #refocus-btn { background: none; border: 1px solid var(--border); border-radius: 4px; color: var(--muted); cursor: pointer; font-size: 13px; line-height: 1; padding: 3px 7px; transition: color 0.15s, border-color 0.15s; }
  #refocus-btn:hover { color: var(--accent); border-color: var(--accent); }

  .panel-body { flex: 1; overflow-y: auto; padding: 18px; display: flex; flex-direction: column; gap: 14px; }
  .panel-body::-webkit-scrollbar { width: 4px; }
  .panel-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .section-title { font-family: 'DM Mono', monospace; font-size: 10px; font-weight: 500; color: var(--accent); text-transform: uppercase; letter-spacing: 0.12em; padding: 6px 0 2px; border-top: 1px solid var(--border); margin-top: 4px; }

  .field-group { display: flex; flex-direction: column; gap: 5px; }
  .field-label { font-family: 'DM Mono', monospace; font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }

  input[type="text"], textarea, select {
    width: 100%; background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-family: 'DM Sans', sans-serif;
    font-size: 13px; padding: 7px 10px; outline: none; transition: border-color 0.2s; resize: vertical;
  }
  input:focus, textarea:focus, select:focus { border-color: var(--accent); }
  select option { background: var(--surface2); }

  /* Dynamic list rows (relations, addresses) */
  .list-item { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 6px; position: relative; }
  .list-item-row { display: flex; gap: 6px; }
  .list-item-row input, .list-item-row select { flex: 1; }
  .remove-btn { position: absolute; top: 8px; right: 8px; background: none; border: none; color: var(--muted); cursor: pointer; font-size: 16px; line-height: 1; padding: 0 4px; flex: none; }
  .remove-btn:hover { color: var(--danger); }
  .add-btn { background: var(--surface2); border: 1px dashed var(--border); border-radius: 6px; color: var(--muted); font-family: 'DM Mono', monospace; font-size: 11px; padding: 7px; cursor: pointer; text-align: center; transition: border-color 0.2s, color 0.2s; width: 100%; }
  .rel-name-wrap { position: relative; flex: 1; }
  .rel-autocomplete {
    position: absolute; top: calc(100% + 2px); left: 0; right: 0; z-index: 200;
    background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.4); max-height: 160px; overflow-y: auto; display: none;
  }
  .rel-autocomplete-item {
    padding: 7px 10px; font-size: 12px; cursor: pointer; color: var(--text);
    border-bottom: 1px solid var(--border); transition: background 0.1s;
  }
  .rel-autocomplete-item:last-child { border-bottom: none; }
  .rel-autocomplete-item:hover, .rel-autocomplete-item.active { background: var(--surface2); }
  .rel-autocomplete-item mark { background: none; color: var(--accent); font-weight: 600; }
  .add-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* Group checkboxes */
  .group-list { display: flex; flex-direction: column; gap: 6px; }
  .group-check { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; }
  .group-check input[type="checkbox"] { accent-color: var(--accent2); width: 14px; height: 14px; flex-shrink: 0; }

  .divider { height: 1px; background: var(--border); }

  .panel-footer { padding: 14px 18px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; }
  .footer-btns { display: flex; gap: 8px; }
  button { padding: 9px 14px; border-radius: 6px; font-family: 'DM Mono', monospace; font-size: 12px; font-weight: 500; cursor: pointer; border: none; transition: opacity 0.15s; }
  #save-btn { flex: 1; background: var(--accent); color: #fff; }
  #save-btn:hover { opacity: 0.85; }
  #save-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #cancel-btn { flex: 1; background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }
  #cancel-btn:hover { color: var(--text); }

  .field-error { font-family: 'DM Mono', monospace; font-size: 10px; color: var(--danger); margin-top: 3px; display: none; }
  .field-error.visible { display: block; }
  input.invalid, textarea.invalid, select.invalid { border-color: var(--danger) !important; }
  .status-msg { font-family: 'DM Mono', monospace; font-size: 11px; text-align: center; padding: 6px; border-radius: 4px; display: none; }
  .status-msg.success { background: rgba(62,207,142,0.12); color: var(--success); display: block; }
  .status-msg.error   { background: rgba(232,91,91,0.12);  color: var(--danger);  display: block; }

  /* Search */
  .search-wrap { position: relative; margin-left: 24px; }
  #search-input {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-family: 'DM Mono', monospace; font-size: 12px;
    padding: 6px 12px 6px 30px; outline: none; width: 260px; transition: border-color 0.2s;
  }
  #search-input:focus { border-color: var(--accent); }
  #search-input::placeholder { color: var(--muted); }
  .search-icon { position: absolute; left: 9px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 13px; pointer-events: none; }
  #search-results {
    position: absolute; top: calc(100% + 6px); left: 0; width: 280px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4); z-index: 100; display: none;
    max-height: 280px; overflow-y: auto;
  }
  #search-results::-webkit-scrollbar { width: 4px; }
  #search-results::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .search-result {
    padding: 9px 14px; cursor: pointer; font-size: 13px;
    border-bottom: 1px solid var(--border); transition: background 0.15s;
    display: flex; flex-direction: column; gap: 2px;
  }
  .search-result:last-child { border-bottom: none; }
  .search-result:hover, .search-result.active { background: var(--surface2); }
  .search-result-name { color: var(--text); font-family: 'DM Sans', sans-serif; }
  .search-result-sub { color: var(--muted); font-size: 11px; font-family: 'DM Mono', monospace; }
  .search-result mark { background: none; color: var(--accent); font-weight: 600; }
  .search-empty { padding: 12px 14px; color: var(--muted); font-size: 12px; font-family: 'DM Mono', monospace; }
  .search-result-group { border-left: 2px solid var(--accent2); }
  .search-section-header {
    padding: 6px 14px 4px; font-family: 'DM Mono', monospace; font-size: 10px;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em;
    background: var(--surface2); border-bottom: 1px solid var(--border);
  }

  #loading { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; z-index: 999; }
  .spinner { width: 36px; height: 36px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #loading p { font-family: 'DM Mono', monospace; font-size: 13px; color: var(--muted); }
</style>
</head>
<body>

<div id="loading"><div class="spinner"></div><p>loading contacts...</p></div>

<header>
  <div class="dot"></div>
  <h1>contact_map</h1>
  <div class="search-wrap">
    <span class="search-icon">⌕</span>
    <input id="search-input" type="text" placeholder="Search contacts & groups...">
    <div id="search-results"></div>
  </div>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#5b8dee"></div><span>person</span></div>
    <div class="legend-item"><div class="legend-dot" style="background:#e85b8d"></div><span>group</span></div>
    <div class="legend-item">
      <svg width="20" height="10"><line x1="0" y1="5" x2="20" y2="5" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4,4"/></svg>
      <span>relationship</span>
    </div>
  </div>
</header>

<div class="main">
  <div id="graph"></div>
  <div id="panel">
    <div class="panel-header">
      <h2 id="panel-title">Contact</h2>
      <div style="display:flex;gap:6px;align-items:center;">
        <button id="refocus-btn" title="Snap back to this node">⌖</button>
        <button id="close-panel">×</button>
      </div>
    </div>
    <div class="panel-body" id="panel-body"></div>
    <div class="panel-footer">
      <div class="footer-btns">
        <button id="cancel-btn">Cancel</button>
        <button id="save-btn">Save Changes</button>
      </div>
      <div class="status-msg" id="status-msg"></div>
    </div>
  </div>
</div>

<script>
const API = 'http://localhost:5000/api';
let network, allContacts = {}, allGroups = {}, currentContact = null;

const RELATION_TYPES = ['spouse','child','mother','father','parent','brother','sister','friend','colleague','manager','assistant','partner','referred_by','domestic_partner','relative','other'];
const ADDRESS_TYPES  = ['home','work','other'];

// ── Load & build graph ───────────────────────────────────────────────────────
async function reloadGraph() {
  const res  = await fetch(`${API}/data`);
  const data = await res.json();
  allGroups  = data.groups;
  allContacts = {};
  data.contacts.forEach(c => allContacts[c.resourceName] = c);

  const nodes = new vis.DataSet();
  const edges = new vis.DataSet();

  Object.entries(allGroups).forEach(([rn, g]) => {
    if ((g.memberCount||0) === 0) return;
    nodes.add({ id: rn, label: g.name, type: 'group',
      color: { background: '#1f1529', border: '#e85b8d', highlight: { background: '#2a1a38', border: '#e85b8d' } },
      font: { color: '#e85b8d', size: 14, face: 'DM Mono' }, shape: 'dot', size: 34, borderWidth: 2.5 });
  });

  data.contacts.forEach(c => {
    nodes.add({ id: c.resourceName, label: c.name, type: 'person',
      color: { background: '#111a2e', border: '#5b8dee', highlight: { background: '#162240', border: '#5b8dee' } },
      font: { color: '#e2e8f8', size: 12, face: 'DM Sans' }, shape: 'dot', size: 14, borderWidth: 1.5 });
    c.groups.forEach(grn => {
      if (allGroups[grn] && (allGroups[grn].memberCount||0) > 0) {
        edges.add({ from: c.resourceName, to: grn,
          color: { color: '#3d6fd4', highlight: '#7aabff', opacity: 0.9 }, width: 2.5, smooth: { type: 'continuous' } });
      }
    });
    // Relationship edges — dotted lines between contacts
    (c.relations||[]).forEach(r => {
      if (!r.name) return;
      // Find the contact whose name matches the relation
      const relContact = Object.values(allContacts).find(x =>
        x.name.toLowerCase() === r.name.toLowerCase()
      );
      if (relContact) {
        // Use a unique edge id to avoid duplicates
        const edgeId = [c.resourceName, relContact.resourceName].sort().join('--rel--');
        if (!edges.get(edgeId)) {
          edges.add({
            id: edgeId,
            from: c.resourceName,
            to: relContact.resourceName,
            color: { color: '#f59e0b', highlight: '#fde68a', opacity: 0.9 },
            width: 2.5,
            dashes: [5, 5],
            smooth: { type: 'curvedCW', roundness: 0.2 },
            arrows: { to: { enabled: true, scaleFactor: 0.6 } },
            label: '',
            _relType: r.type,
            font: { color: '#fde68a', size: 11, face: 'DM Mono', background: 'rgba(13,15,20,0.85)', strokeWidth: 0 },
          });
        }
      }
    });
  });

  network.setData({ nodes, edges });
}

async function loadGraph() {
  const res  = await fetch(`${API}/data`);
  const data = await res.json();
  allGroups  = data.groups;
  data.contacts.forEach(c => allContacts[c.resourceName] = c);

  const nodes = new vis.DataSet();
  const edges = new vis.DataSet();

  Object.entries(allGroups).forEach(([rn, g]) => {
    if ((g.memberCount||0) === 0) return; // empty groups don't appear in graph
    nodes.add({ id: rn, label: g.name, type: 'group',
      color: { background: '#1f1529', border: '#e85b8d', highlight: { background: '#2a1a38', border: '#e85b8d' } },
      font: { color: '#e85b8d', size: 14, face: 'DM Mono' }, shape: 'dot', size: 34, borderWidth: 2.5 });
  });

  data.contacts.forEach(c => {
    nodes.add({ id: c.resourceName, label: c.name, type: 'person',
      color: { background: '#111a2e', border: '#5b8dee', highlight: { background: '#162240', border: '#5b8dee' } },
      font: { color: '#e2e8f8', size: 12, face: 'DM Sans' }, shape: 'dot', size: 14, borderWidth: 1.5 });
    c.groups.forEach(grn => {
      if (allGroups[grn]) edges.add({ from: c.resourceName, to: grn,
        color: { color: '#3d6fd4', highlight: '#7aabff', opacity: 0.9 }, width: 2.5, smooth: { type: 'continuous' } });
    });
    // Relationship edges — dotted lines between contacts
    (c.relations||[]).forEach(r => {
      if (!r.name) return;
      // Find the contact whose name matches the relation
      const relContact = Object.values(allContacts).find(x =>
        x.name.toLowerCase() === r.name.toLowerCase()
      );
      if (relContact) {
        // Use a unique edge id to avoid duplicates
        const edgeId = [c.resourceName, relContact.resourceName].sort().join('--rel--');
        if (!edges.get(edgeId)) {
          edges.add({
            id: edgeId,
            from: c.resourceName,
            to: relContact.resourceName,
            color: { color: '#f59e0b', highlight: '#fde68a', opacity: 0.9 },
            width: 2.5,
            dashes: [5, 5],
            smooth: { type: 'curvedCW', roundness: 0.2 },
            arrows: { to: { enabled: true, scaleFactor: 0.6 } },
            label: '',
            _relType: r.type,
            font: { color: '#fde68a', size: 11, face: 'DM Mono', background: 'rgba(13,15,20,0.85)', strokeWidth: 0 },
          });
        }
      }
    });
  });

  network = new vis.Network(document.getElementById('graph'), { nodes, edges }, {
    physics: { stabilization: { iterations: 150 }, barnesHut: { gravitationalConstant: -8000, springLength: 120, springConstant: 0.04 } },
    interaction: { hover: true, tooltipDelay: 300 },
    nodes: {
      chosen: {
        node: (values, id, selected, hovering) => {
          const isContact = !!allContacts[id];
          if (hovering && !selected) {
            // Solid fill on hover
            values.color       = isContact ? '#5b8dee' : '#e85b8d';
            values.size        = values.size * 1.3;
            values.shadowSize  = 14;
            values.shadowColor = isContact ? 'rgba(91,141,238,0.4)' : 'rgba(232,91,141,0.4)';
            values.shadowX     = 0;
            values.shadowY     = 0;
          }
          if (selected) {
            values.color       = isContact ? '#5b8dee' : '#e85b8d';
            values.size        = values.size * 1.5;
            values.borderWidth = 4;
            values.shadowSize  = 24;
            values.shadowColor = isContact ? 'rgba(91,141,238,0.8)' : 'rgba(232,91,141,0.8)';
            values.shadowX     = 0;
            values.shadowY     = 0;
          }
        }
      }
    },
  });

  network.on('click', params => {
    // Edge click — toggle label
    if (params.edges.length && !params.nodes.length) {
      params.edges.forEach(edgeId => {
        const edge = network.body.data.edges.get(edgeId);
        if (edge && edge._relType) {
          const showing = edge.label === edge._relType;
          network.body.data.edges.update({ id: edgeId, label: showing ? '' : edge._relType });
        }
      });
      return;
    }
    if (!params.nodes.length) {
      // Clicking empty space — hide all edge labels, deselect, close panel
      network.body.data.edges.forEach(e => {
        if (e._relType && e.label) network.body.data.edges.update({ id: e.id, label: '' });
      });
      network.unselectAll();
      closePanel();
      return;
    }
    const id = params.nodes[0];
    const contact = allContacts[id];
    if (contact) {
      openPanel(contact);
    } else {
      // Group node clicked — deselect and close panel
      network.unselectAll();
      closePanel();
    }
  });

  network.on('hoverNode', () => { document.getElementById('graph').style.cursor = 'pointer'; });
  network.on('blurNode',  () => { document.getElementById('graph').style.cursor = 'default'; });

  document.getElementById('loading').style.display = 'none';
}

// ── Panel builder ────────────────────────────────────────────────────────────
function openPanel(contact) {
  currentContact = contact;
  document.getElementById('panel-title').textContent = contact.name;
  const body = document.getElementById('panel-body');
  body.innerHTML = '';

  // ── Basic fields
  body.appendChild(sectionTitle('Basic Info'));
  body.appendChild(textField('Full Name', 'name', contact.name));
  body.appendChild(textField('Nickname', 'nickname', (contact.nicknames||[])[0]||'', 'e.g. Johnny'));
  body.appendChild(textField('Email(s)', 'emails', (contact.emails||[]).join(', '), 'email1@x.com, email2@x.com'));
  body.appendChild(textField('Phone(s)', 'phones', (contact.phones||[]).map(p=>p.number).join(', '), '+1 555 000 0000'));
  body.appendChild(textareaField('Notes / Bio', 'bio', contact.bio||''));

  // ── Significant Dates section
  body.appendChild(sectionTitle('Significant Dates'));
  const datesContainer = document.createElement('div');
  datesContainer.id = 'dates-container';
  datesContainer.style.display = 'flex'; datesContainer.style.flexDirection = 'column'; datesContainer.style.gap = '8px';
  // Birthday always first
  datesContainer.appendChild(dateRow({ type: 'birthday', date: contact.birthday||'' }));
  // Other events
  (contact.events||[]).forEach(e => {
    const dateStr = e.date ? `${e.date.year||'????'}-${String(e.date.month||'??').padStart(2,'0')}-${String(e.date.day||'??').padStart(2,'0')}` : '';
    datesContainer.appendChild(dateRow({ type: e.type||'anniversary', date: dateStr }));
  });
  body.appendChild(datesContainer);
  const addDateBtn = document.createElement('button');
  addDateBtn.className = 'add-btn'; addDateBtn.textContent = '+ Add Date';
  addDateBtn.onclick = () => datesContainer.appendChild(dateRow({ type: 'anniversary', date: '' }));
  body.appendChild(addDateBtn);

  // ── Address section
  body.appendChild(sectionTitle('Addresses'));
  const addrContainer = document.createElement('div');
  addrContainer.id = 'addr-container';
  addrContainer.style.display = 'flex'; addrContainer.style.flexDirection = 'column'; addrContainer.style.gap = '8px';
  (contact.addresses||[]).forEach(a => addrContainer.appendChild(addressRow(a)));
  body.appendChild(addrContainer);
  const addAddrBtn = document.createElement('button');
  addAddrBtn.className = 'add-btn'; addAddrBtn.textContent = '+ Add Address';
  addAddrBtn.onclick = () => addrContainer.appendChild(addressRow({}));
  body.appendChild(addAddrBtn);

  // ── Relationships section
  body.appendChild(sectionTitle('Relationships'));
  const relContainer = document.createElement('div');
  relContainer.id = 'rel-container';
  relContainer.style.display = 'flex'; relContainer.style.flexDirection = 'column'; relContainer.style.gap = '8px';
  (contact.relations||[]).forEach(r => relContainer.appendChild(relationRow(r)));
  body.appendChild(relContainer);
  const addRelBtn = document.createElement('button');
  addRelBtn.className = 'add-btn'; addRelBtn.textContent = '+ Add Relationship';
  addRelBtn.onclick = () => relContainer.appendChild(relationRow({}));
  body.appendChild(addRelBtn);

  // ── Groups section
  body.appendChild(sectionTitle('Groups'));
  const groupList = document.createElement('div');
  groupList.className = 'group-list'; groupList.id = 'group-list';
  function renderGroupList() {
    groupList.innerHTML = '';
    // Separate empty vs populated groups
    const populated = Object.entries(allGroups).filter(([,g]) => (g.memberCount||0) > 0);
    const empty     = Object.entries(allGroups).filter(([,g]) => (g.memberCount||0) === 0);

    function makeRow(rn, g) {
      const gname = g.name;
      const count = g.memberCount || 0;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;';
      const label = document.createElement('label'); label.className = 'group-check'; label.style.flex = '1';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = rn;
      cb.checked = (currentContact.groups||[]).includes(rn);
      label.appendChild(cb);
      label.appendChild(document.createTextNode(gname));
      // Member count badge
      const badge = document.createElement('span');
      badge.textContent = count === 0 ? 'empty' : count;
      badge.style.cssText = count === 0
        ? 'font-family:DM Mono,monospace;font-size:10px;color:var(--danger);opacity:0.8;flex:none;'
        : 'font-family:DM Mono,monospace;font-size:10px;color:var(--muted);flex:none;';
      const delBtn = document.createElement('button');
      delBtn.textContent = '×'; delBtn.title = 'Delete this group';
      delBtn.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0 4px;flex:none;';
      delBtn.onmouseover = () => delBtn.style.color = 'var(--danger)';
      delBtn.onmouseout  = () => delBtn.style.color = 'var(--muted)';
      delBtn.onclick = async () => {
        if (!confirm(`Delete group "${gname}"? This will remove it from all contacts.`)) return;
        const res = await fetch(`${API}/delete_group`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({resourceName: rn})});
        const d = await res.json();
        if (d.success) {
          delete allGroups[rn];
          currentContact.groups = (currentContact.groups||[]).filter(x => x !== rn);
          currentContact.groupNames = (currentContact.groupNames||[]).filter(n => n !== gname);
          renderGroupList();
        } else { alert('Failed to delete group: ' + d.error); }
      };
      row.appendChild(label); row.appendChild(badge); row.appendChild(delBtn);
      return row;
    }

    populated.forEach(([rn, g]) => groupList.appendChild(makeRow(rn, g)));

    if (empty.length) {
      const emptyHeader = document.createElement('div');
      emptyHeader.style.cssText = 'font-family:DM Mono,monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);';
      emptyHeader.textContent = 'Empty groups';
      groupList.appendChild(emptyHeader);
      empty.forEach(([rn, g]) => groupList.appendChild(makeRow(rn, g)));
    }
  }
  renderGroupList();
  body.appendChild(groupList);

  // Create new group inline
  const newGroupRow = document.createElement('div');
  newGroupRow.style.cssText = 'display:flex;gap:6px;margin-top:4px;';
  const newGroupInput = document.createElement('input');
  newGroupInput.type = 'text'; newGroupInput.placeholder = 'New group name...';
  newGroupInput.style.cssText = 'flex:1;background:var(--surface2);border:1px dashed var(--border);border-radius:6px;color:var(--text);font-size:13px;padding:7px 10px;outline:none;';
  newGroupInput.onfocus = () => newGroupInput.style.borderColor = 'var(--accent)';
  newGroupInput.onblur  = () => newGroupInput.style.borderColor = 'var(--border)';
  const newGroupBtn = document.createElement('button');
  newGroupBtn.textContent = '+ Create';
  newGroupBtn.style.cssText = 'background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--muted);font-family:DM Mono,monospace;font-size:11px;padding:7px 12px;cursor:pointer;white-space:nowrap;';
  newGroupBtn.onmouseover = () => { newGroupBtn.style.borderColor='var(--accent)'; newGroupBtn.style.color='var(--accent)'; };
  newGroupBtn.onmouseout  = () => { newGroupBtn.style.borderColor='var(--border)'; newGroupBtn.style.color='var(--muted)'; };
  newGroupBtn.onclick = async () => {
    const name = newGroupInput.value.trim();
    if (!name) return;
    newGroupBtn.textContent = '...'; newGroupBtn.disabled = true;
    const res = await fetch(`${API}/create_group`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
    const d = await res.json();
    if (d.success) {
      // Store with correct shape {name, system}
      allGroups[d.resourceName] = { name: d.name, memberCount: 1 };
      // Auto-add the current contact to the new group
      if (currentContact) {
        try {
          await fetch(`${API}/update`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              resourceName: currentContact.resourceName,
              groups: [...(currentContact.groups||[]), d.resourceName]
            })
          });
          currentContact.groups = [...(currentContact.groups||[]), d.resourceName];
          currentContact.groupNames = [...(currentContact.groupNames||[]), d.name];
        } catch(e) { console.error('Failed to add contact to new group', e); }
      }
      newGroupInput.value = '';
      renderGroupList();
    } else { alert('Failed to create group: ' + d.error); }
    newGroupBtn.textContent = '+ Create'; newGroupBtn.disabled = false;
  };
  newGroupInput.onkeydown = e => { if (e.key === 'Enter') newGroupBtn.click(); };
  newGroupRow.appendChild(newGroupInput); newGroupRow.appendChild(newGroupBtn);
  body.appendChild(newGroupRow);

  // ── Read-only org
  if (contact.organizations?.length) {
    body.appendChild(sectionTitle('Organization'));
    const org = contact.organizations[0];
    body.appendChild(readOnly([org.title, org.name, org.department].filter(Boolean).join(' · ')));
  }

  clearErrors();
  document.getElementById('panel').classList.add('open');
  document.getElementById('save-btn').disabled = false;
  document.getElementById('status-msg').className = 'status-msg';
}

// ── Field helpers ─────────────────────────────────────────────────────────────
function sectionTitle(text) {
  const d = document.createElement('div'); d.className = 'section-title'; d.textContent = text; return d;
}
function textField(label, key, value, placeholder='') {
  const g = document.createElement('div'); g.className = 'field-group';
  const l = document.createElement('div'); l.className = 'field-label'; l.textContent = label;
  const i = document.createElement('input'); i.type = 'text'; i.value = value; i.placeholder = placeholder; i.dataset.key = key;
  const err = document.createElement('div'); err.className = 'field-error'; err.dataset.errorFor = key;
  g.appendChild(l); g.appendChild(i); g.appendChild(err); return g;
}
function textareaField(label, key, value) {
  const g = document.createElement('div'); g.className = 'field-group';
  const l = document.createElement('div'); l.className = 'field-label'; l.textContent = label;
  const t = document.createElement('textarea'); t.rows = 3; t.value = value; t.dataset.key = key;
  g.appendChild(l); g.appendChild(t); return g;
}
function readOnly(value) {
  const d = document.createElement('div'); d.style.fontSize = '13px'; d.style.color = '#6b7494'; d.textContent = value || '—'; return d;
}

function addressRow(a) {
  const item = document.createElement('div'); item.className = 'list-item';
  const removeBtn = document.createElement('button'); removeBtn.className = 'remove-btn'; removeBtn.textContent = '×';
  removeBtn.onclick = () => item.remove();
  item.appendChild(removeBtn);

  const fields = [
    ['Street', 'street', a.street||''],
    ['City', 'city', a.city||''],
    ['Region / State', 'region', a.region||''],
    ['Country', 'country', a.country||''],
    ['Postal Code', 'postalCode', a.postalCode||''],
  ];
  fields.forEach(([label, key, val]) => {
    const row = document.createElement('div'); row.className = 'list-item-row';
    const inp = document.createElement('input'); inp.type = 'text'; inp.placeholder = label; inp.value = val; inp.dataset.addrField = key;
    row.appendChild(inp); item.appendChild(row);
  });

  // Type selector
  const typeRow = document.createElement('div'); typeRow.className = 'list-item-row';
  const sel = document.createElement('select'); sel.dataset.addrField = 'type';
  ADDRESS_TYPES.forEach(t => { const o = document.createElement('option'); o.value = t; o.textContent = t; if (t === (a.type||'home')) o.selected = true; sel.appendChild(o); });
  typeRow.appendChild(sel); item.appendChild(typeRow);
  return item;
}

const DATE_TYPES = ['birthday', 'anniversary', 'other'];

function dateRow(d) {
  const item = document.createElement('div'); item.className = 'list-item';
  const isBirthday = d.type === 'birthday';

  // Only non-birthday rows can be removed
  if (!isBirthday) {
    const removeBtn = document.createElement('button'); removeBtn.className = 'remove-btn'; removeBtn.textContent = '×';
    removeBtn.onclick = () => item.remove();
    item.appendChild(removeBtn);
  }

  const row = document.createElement('div'); row.className = 'list-item-row';

  // Type selector
  const sel = document.createElement('select'); sel.dataset.dateField = 'type';
  DATE_TYPES.forEach(t => {
    const o = document.createElement('option'); o.value = t; o.textContent = t.charAt(0).toUpperCase() + t.slice(1);
    if (t === (d.type||'anniversary')) o.selected = true;
    sel.appendChild(o);
  });
  if (isBirthday) { sel.disabled = true; sel.style.opacity = '0.6'; }

  // Date input
  const inp = document.createElement('input'); inp.type = 'text';
  inp.placeholder = 'YYYY-MM-DD or ????-MM-DD';
  inp.value = d.date || '';
  inp.dataset.dateField = 'date';
  if (isBirthday) inp.dataset.isBirthday = 'true';

  row.appendChild(sel); row.appendChild(inp); item.appendChild(row);

  // Inline error
  const err = document.createElement('div'); err.className = 'field-error'; err.dataset.errorFor = 'date-' + Math.random();
  item.appendChild(err);
  item._dateErr = err;

  return item;
}

function relationRow(r) {
  const item = document.createElement('div'); item.className = 'list-item';
  const removeBtn = document.createElement('button'); removeBtn.className = 'remove-btn'; removeBtn.textContent = '×';
  removeBtn.onclick = () => item.remove();
  item.appendChild(removeBtn);

  const row = document.createElement('div'); row.className = 'list-item-row';

  // Name input with autocomplete
  const nameWrap = document.createElement('div'); nameWrap.className = 'rel-name-wrap';
  const nameInp = document.createElement('input'); nameInp.type = 'text'; nameInp.placeholder = 'Search contact...';
  nameInp.value = r.name||''; nameInp.dataset.relField = 'name';
  const dropdown = document.createElement('div'); dropdown.className = 'rel-autocomplete';
  nameWrap.appendChild(nameInp); nameWrap.appendChild(dropdown);

  let acIndex = -1;

  function showSuggestions(query) {
    dropdown.innerHTML = ''; acIndex = -1;
    if (!query.trim()) { dropdown.style.display = 'none'; return; }
    const q = query.toLowerCase();
    const matches = Object.values(allContacts)
      .filter(c => c.name.toLowerCase().split(/\s+/).some(w => w.startsWith(q)))
      .slice(0, 8);
    if (!matches.length) { dropdown.style.display = 'none'; return; }
    matches.forEach((c, i) => {
      const opt = document.createElement('div'); opt.className = 'rel-autocomplete-item';
      opt.dataset.idx = i;
      const idx = c.name.toLowerCase().indexOf(q);
      opt.innerHTML = idx >= 0
        ? c.name.slice(0,idx) + '<mark>' + c.name.slice(idx, idx+q.length) + '</mark>' + c.name.slice(idx+q.length)
        : c.name;
      opt.onmousedown = (e) => {
        e.preventDefault();
        nameInp.value = c.name;
        dropdown.style.display = 'none';
      };
      dropdown.appendChild(opt);
    });
    dropdown.style.display = 'block';
  }

  function setAcActive(idx) {
    const items = dropdown.querySelectorAll('.rel-autocomplete-item');
    items.forEach((el, i) => el.classList.toggle('active', i === idx));
    if (items[idx]) items[idx].scrollIntoView({ block: 'nearest' });
  }

  nameInp.addEventListener('input', e => showSuggestions(e.target.value));
  nameInp.addEventListener('keydown', e => {
    const items = dropdown.querySelectorAll('.rel-autocomplete-item');
    if (e.key === 'ArrowDown') { e.preventDefault(); acIndex = Math.min(acIndex+1, items.length-1); setAcActive(acIndex); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); acIndex = Math.max(acIndex-1, 0); setAcActive(acIndex); }
    else if (e.key === 'Enter' && acIndex >= 0) { e.preventDefault(); nameInp.value = items[acIndex].textContent; dropdown.style.display = 'none'; acIndex = -1; }
    else if (e.key === 'Escape') { dropdown.style.display = 'none'; acIndex = -1; }
  });
  nameInp.addEventListener('blur', () => setTimeout(() => { dropdown.style.display = 'none'; }, 150));

  const sel = document.createElement('select'); sel.dataset.relField = 'type';
  RELATION_TYPES.forEach(t => { const o = document.createElement('option'); o.value = t; o.textContent = t; if (t === (r.type||'other')) o.selected = true; sel.appendChild(o); });
  row.appendChild(nameWrap); row.appendChild(sel); item.appendChild(row);
  return item;
}

// ── Collect & save ────────────────────────────────────────────────────────────
function closePanel() {
  document.getElementById('panel').classList.remove('open');
  currentContact = null;
}

function clearErrors() {
  document.querySelectorAll('.field-error').forEach(e => { e.textContent = ''; e.classList.remove('visible'); });
  document.querySelectorAll('.invalid').forEach(e => e.classList.remove('invalid'));
}

function showFieldError(key, msg) {
  const err = document.querySelector(`.field-error[data-error-for="${key}"]`);
  const inp = document.querySelector(`[data-key="${key}"]`);
  if (err) { err.textContent = msg; err.classList.add('visible'); }
  if (inp) inp.classList.add('invalid');
}

function validateFields() {
  clearErrors();
  let valid = true;

  // Name required
  const nameEl = document.querySelector('[data-key="name"]');
  if (nameEl && !nameEl.value.trim()) {
    showFieldError('name', 'Name is required');
    valid = false;
  }

  // Email format
  const emailEl = document.querySelector('[data-key="emails"]');
  if (emailEl && emailEl.value.trim()) {
    const emails = emailEl.value.split(',').map(s => s.trim()).filter(Boolean);
    const emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    const bad = emails.filter(e => !emailRe.test(e));
    if (bad.length) {
      showFieldError('emails', `Invalid email${bad.length > 1 ? 's' : ''}: ${bad.join(', ')}`);
      valid = false;
    }
  }

  // Significant dates validation
  const dateRe = /^(\d{4}|\?{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;
  document.querySelectorAll('#dates-container .list-item').forEach(item => {
    const dateEl = item.querySelector('[data-date-field="date"]');
    if (dateEl && dateEl.value.trim() && !dateRe.test(dateEl.value.trim())) {
      dateEl.classList.add('invalid');
      if (item._dateErr) { item._dateErr.textContent = 'Use YYYY-MM-DD or ????-MM-DD'; item._dateErr.classList.add('visible'); }
      valid = false;
    }
  });

  // Phone — basic check, no letters
  const phoneEl = document.querySelector('[data-key="phones"]');
  if (phoneEl && phoneEl.value.trim()) {
    const phones = phoneEl.value.split(',').map(s => s.trim()).filter(Boolean);
    const bad = phones.filter(p => /[a-zA-Z]/.test(p));
    if (bad.length) {
      showFieldError('phones', `Phone numbers can't contain letters`);
      valid = false;
    }
  }

  // Relationships — name must not be empty if row exists
  let relValid = true;
  document.querySelectorAll('#rel-container .list-item').forEach(item => {
    const nameInp = item.querySelector('[data-rel-field="name"]');
    if (nameInp && !nameInp.value.trim()) {
      nameInp.classList.add('invalid');
      relValid = false;
    }
  });
  if (!relValid) {
    const msg = document.getElementById('status-msg');
    msg.textContent = 'Relationship name cannot be empty — fill it in or remove the row';
    msg.className = 'status-msg error'; msg.style.display = 'block';
    valid = false;
  }

  return valid;
}

async function saveContact() {
  if (!currentContact) return;
  const btn = document.getElementById('save-btn');

  if (!validateFields()) {
    btn.disabled = false; btn.textContent = 'Save Changes';
    return;
  }

  btn.disabled = true; btn.textContent = 'Saving...';

  const payload = { resourceName: currentContact.resourceName };

  // Basic fields — only send if changed
  document.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (key === 'emails') {
      const newVal = el.value.split(',').map(s=>s.trim()).filter(Boolean);
      if (JSON.stringify(newVal) !== JSON.stringify(currentContact.emails||[])) payload.emails = newVal;
    } else if (key === 'phones') {
      const newVal = el.value.split(',').map(s=>({number:s.trim(),type:'mobile'})).filter(p=>p.number);
      const oldVal = (currentContact.phones||[]).map(p=>({number:p.number,type:'mobile'}));
      if (JSON.stringify(newVal) !== JSON.stringify(oldVal)) payload.phones = newVal;
    } else if (key === 'nickname') {
      const newVal = el.value.trim();
      const oldVal = (currentContact.nicknames||[])[0] || '';
      if (newVal !== oldVal) payload.nickname = newVal;
    } else {
      const newVal = el.value.trim();
      if (newVal !== (currentContact[key]||'')) payload[key] = newVal;
    }
  });

  // Addresses — only send if changed
  const newAddresses = [];
  document.querySelectorAll('#addr-container .list-item').forEach(item => {
    const addr = {};
    item.querySelectorAll('[data-addr-field]').forEach(el => { addr[el.dataset.addrField] = el.value.trim(); });
    if (Object.values(addr).some(v => v)) newAddresses.push(addr);
  });
  const oldAddresses = (currentContact.addresses||[]).map(a=>({street:a.street||'',city:a.city||'',region:a.region||'',country:a.country||'',postalCode:a.postalCode||'',type:a.type||'home'}));
  if (JSON.stringify(newAddresses) !== JSON.stringify(oldAddresses)) payload.addresses = newAddresses;

  // Relations — only send if changed
  const newRelations = [];
  document.querySelectorAll('#rel-container .list-item').forEach(item => {
    const rel = {};
    item.querySelectorAll('[data-rel-field]').forEach(el => { rel[el.dataset.relField] = el.value.trim(); });
    if (rel.name) newRelations.push(rel);
  });
  const oldRelations = (currentContact.relations||[]).map(r=>({name:r.name||'',type:r.type||'other'}));
  if (JSON.stringify(newRelations) !== JSON.stringify(oldRelations)) payload.relations = newRelations;

  // Significant dates — birthday + events
  let birthdayVal = null;
  const newEvents = [];
  document.querySelectorAll('#dates-container .list-item').forEach(item => {
    const typeEl = item.querySelector('[data-date-field="type"]');
    const dateEl = item.querySelector('[data-date-field="date"]');
    if (!typeEl || !dateEl) return;
    const type = typeEl.value;
    const dateStr = dateEl.value.trim();
    if (type === 'birthday') {
      birthdayVal = dateStr;
    } else if (dateStr) {
      newEvents.push({ type, date: dateStr });
    }
  });
  // Only include if changed
  if (birthdayVal !== null && birthdayVal !== (currentContact.birthday||'')) payload.birthday = birthdayVal || '';
  const oldEvents = (currentContact.events||[]).map(e => ({
    type: e.type||'anniversary',
    date: e.date ? `${e.date.year||'????'}-${String(e.date.month||'??').padStart(2,'0')}-${String(e.date.day||'??').padStart(2,'0')}` : ''
  }));
  if (JSON.stringify(newEvents) !== JSON.stringify(oldEvents)) payload.events = newEvents;

  // Groups — only send if changed
  const newGroups = [];
  document.querySelectorAll('#group-list input[type="checkbox"]:checked').forEach(cb => { newGroups.push(cb.value); });
  if (JSON.stringify([...newGroups].sort()) !== JSON.stringify([...(currentContact.groups||[])].sort())) payload.groups = newGroups;

  // Nothing changed — skip the API call entirely
  if (Object.keys(payload).length <= 1) {
    const msg = document.getElementById('status-msg');
    msg.textContent = 'No changes detected'; msg.className = 'status-msg success';
    btn.disabled = false; btn.textContent = 'Save Changes';
    return;
  }

  try {
    const res  = await fetch(`${API}/update`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const data = await res.json();
    const msg  = document.getElementById('status-msg');
    if (data.success) {
      msg.textContent = '✓ Saved — reloading graph...';
      msg.className = 'status-msg success';
      Object.assign(allContacts[currentContact.resourceName], payload);
      const savedContact = allContacts[currentContact.resourceName];
      setTimeout(async () => {
        await reloadGraph();
        // Re-open the panel for the same contact with fresh data
        if (allContacts[savedContact.resourceName]) {
          openPanel(allContacts[savedContact.resourceName]);
        }
      }, 800);
    } else {
      msg.textContent = data.error || 'Something went wrong';
      msg.className = 'status-msg error';
    }
  } catch(e) {
    const msg = document.getElementById('status-msg');
    msg.textContent = 'Could not reach local server';
    msg.className = 'status-msg error';
  }

  btn.disabled = false; btn.textContent = 'Save Changes';
}

window.addEventListener('resize', () => { if (network) network.redraw(); });
// Live inline validation on change
document.addEventListener('input', e => {
  const key = e.target.dataset.key;
  if (!key) return;
  const err = document.querySelector(`.field-error[data-error-for="${key}"]`);
  if (err && err.classList.contains('visible')) {
    // Re-validate just this field on change
    validateFields();
  }
});

document.getElementById('refocus-btn').addEventListener('click', () => {
  if (currentContact) {
    network.focus(currentContact.resourceName, {
      scale: 1.5,
      animation: { duration: 600, easingFunction: 'easeInOutQuad' }
    });
    network.selectNodes([currentContact.resourceName]);
  }
});
document.getElementById('close-panel').addEventListener('click', closePanel);
document.getElementById('cancel-btn').addEventListener('click', closePanel);
document.getElementById('save-btn').addEventListener('click', saveContact);

// ── Search ───────────────────────────────────────────────────────────────────
const searchInput   = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');

function highlight(text, query) {
  if (!query) return text;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return text;
  return text.slice(0, idx) + '<mark>' + text.slice(idx, idx + query.length) + '</mark>' + text.slice(idx + query.length);
}

let searchIndex = -1;

function selectContactResult(item) {
  network.focus(item.dataset.rn, { scale: 1.5, animation: { duration: 500, easingFunction: 'easeInOutQuad' } });
  network.selectNodes([item.dataset.rn]);
  openPanel(allContacts[item.dataset.rn]);
  searchInput.value = '';
  searchResults.style.display = 'none';
  searchIndex = -1;
}

function selectGroupResult(item) {
  const rn = item.dataset.rn;
  if (allGroups[rn] && (allGroups[rn].memberCount||0) > 0) {
    network.focus(rn, { scale: 1.5, animation: { duration: 500, easingFunction: 'easeInOutQuad' } });
    const memberRns = Object.values(allContacts)
      .filter(c => (c.groups||[]).includes(rn))
      .map(c => c.resourceName);
    network.selectNodes([rn, ...memberRns]);
  }
  searchInput.value = '';
  searchResults.style.display = 'none';
  searchIndex = -1;
}

function setActiveResult(idx) {
  const items = searchResults.querySelectorAll('.search-result');
  items.forEach((el, i) => {
    el.classList.toggle('active', i === idx);
    if (i === idx) el.scrollIntoView({ block: 'nearest' });
  });
}

function doSearch(query) {
  searchResults.innerHTML = '';
  searchIndex = -1;
  if (!query.trim()) { searchResults.style.display = 'none'; return; }

  const q = query.toLowerCase();
  function matchesWordStart(text) {
    return text.toLowerCase().split(/\s+/).some(word => word.startsWith(q));
  }

  // Match groups
  const groupMatches = Object.entries(allGroups).filter(([, g]) =>
    g.name.toLowerCase().split(/\s+/).some(w => w.startsWith(q))
  );

  // Match contacts
  const contactMatches = Object.values(allContacts).filter(c =>
    matchesWordStart(c.name) ||
    (c.nicknames||[]).some(n => matchesWordStart(n)) ||
    (c.emails||[]).some(e => matchesWordStart(e)) ||
    (c.phones||[]).some(p => p.number.includes(q))
  ).slice(0, 10);

  if (!groupMatches.length && !contactMatches.length) {
    searchResults.innerHTML = '<div class="search-empty">No results found</div>';
    searchResults.style.display = 'block';
    return;
  }

  // Groups section
  if (groupMatches.length) {
    const header = document.createElement('div');
    header.className = 'search-section-header';
    header.textContent = 'Groups';
    searchResults.appendChild(header);
    groupMatches.forEach(([rn, g]) => {
      const item = document.createElement('div');
      item.className = 'search-result search-result-group';
      item.dataset.rn = rn;
      item.dataset.type = 'group';
      const count = g.memberCount || 0;
      item.innerHTML = `<span class="search-result-name">${highlight(g.name, query)}</span>
                        <span class="search-result-sub">${count} member${count !== 1 ? 's' : ''}</span>`;
      item.onclick = () => selectGroupResult(item);
      searchResults.appendChild(item);
    });
  }

  // Contacts section
  if (contactMatches.length) {
    const header = document.createElement('div');
    header.className = 'search-section-header';
    header.textContent = 'Contacts';
    searchResults.appendChild(header);
    contactMatches.forEach(c => {
      const item = document.createElement('div');
      item.className = 'search-result';
      item.dataset.rn = c.resourceName;
      item.dataset.type = 'contact';
      const sub = c.emails[0] || (c.phones[0] && c.phones[0].number) || (c.groupNames||[]).join(', ') || '';
      item.innerHTML = `<span class="search-result-name">${highlight(c.name, query)}</span>
                        <span class="search-result-sub">${highlight(sub, query)}</span>`;
      item.onclick = () => selectContactResult(item);
      searchResults.appendChild(item);
    });
  }

  searchResults.style.display = 'block';
}

searchInput.addEventListener('input', e => doSearch(e.target.value));
searchInput.addEventListener('keydown', e => {
  const items = searchResults.querySelectorAll('.search-result');
  if (e.key === 'Escape') {
    searchResults.style.display = 'none';
    searchInput.blur();
    searchIndex = -1;
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    searchIndex = Math.min(searchIndex + 1, items.length - 1);
    setActiveResult(searchIndex);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    searchIndex = Math.max(searchIndex - 1, 0);
    setActiveResult(searchIndex);
  } else if (e.key === 'Tab') {
    if (!items.length) return;
    e.preventDefault();
    if (e.shiftKey) {
      searchIndex = Math.max(searchIndex - 1, 0);
    } else {
      searchIndex = Math.min(searchIndex + 1, items.length - 1);
    }
    setActiveResult(searchIndex);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const active = searchIndex >= 0 ? items[searchIndex] : (items.length === 1 ? items[0] : null);
    if (active) {
      active.dataset.type === 'group' ? selectGroupResult(active) : selectContactResult(active);
    }
  }
});
document.addEventListener('click', e => {
  if (!e.target.closest('.search-wrap')) { searchResults.style.display = 'none'; searchIndex = -1; }
});
// Ctrl+K or / to focus search
document.addEventListener('keydown', e => {
  if ((e.ctrlKey && e.key === 'k') || (!e.target.closest('input, textarea') && e.key === '/')) {
    e.preventDefault(); searchInput.focus(); searchInput.select();
  }
});

loadGraph();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return GRAPH_HTML


# ─── Main ─────────────────────────────────────────────────────────────────────

def open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open("http://localhost:5000")

if __name__ == "__main__":
    print("Authenticating...")
    creds = authenticate()
    service = build("people", "v1", credentials=creds)
    print("✓ Authenticated")
    print("✓ Starting server at http://localhost:5000")
    print("  (Opening browser automatically...)\n")
    print("  Press Ctrl+C to stop.\n")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(port=5000, debug=False)
