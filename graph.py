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

        if "birthday" in data and data["birthday"]:
            parts = data["birthday"].split("-")
            if len(parts) == 3:
                try:
                    body["birthdays"] = [{"date": {"year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])}}]
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

  .main { display: flex; flex: 1; overflow: hidden; }
  #graph { flex: 1; background: var(--bg); }

  /* Panel */
  #panel { width: 360px; background: var(--surface); border-left: 1px solid var(--border); display: flex; flex-direction: column; transform: translateX(100%); transition: transform 0.3s cubic-bezier(0.4,0,0.2,1); flex-shrink: 0; }
  #panel.open { transform: translateX(0); }

  .panel-header { padding: 18px 20px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .panel-header h2 { font-family: 'DM Mono', monospace; font-size: 12px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }
  #close-panel { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 20px; line-height: 1; padding: 2px 6px; border-radius: 4px; }
  #close-panel:hover { color: var(--text); background: var(--surface2); }

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

  .status-msg { font-family: 'DM Mono', monospace; font-size: 11px; text-align: center; padding: 6px; border-radius: 4px; display: none; }
  .status-msg.success { background: rgba(62,207,142,0.12); color: var(--success); display: block; }
  .status-msg.error   { background: rgba(232,91,91,0.12);  color: var(--danger);  display: block; }

  /* Search */
  .search-wrap { position: relative; margin-left: 24px; }
  #search-input {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-family: 'DM Mono', monospace; font-size: 12px;
    padding: 6px 12px 6px 30px; outline: none; width: 220px; transition: border-color 0.2s;
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
    <input id="search-input" type="text" placeholder="Search contacts...">
    <div id="search-results"></div>
  </div>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#5b8dee"></div><span>person</span></div>
    <div class="legend-item"><div class="legend-dot" style="background:#e85b8d"></div><span>group</span></div>
  </div>
</header>

<div class="main">
  <div id="graph"></div>
  <div id="panel">
    <div class="panel-header">
      <h2 id="panel-title">Contact</h2>
      <button id="close-panel">×</button>
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
      font: { color: '#e85b8d', size: 13, face: 'DM Mono' }, shape: 'dot', size: 22, borderWidth: 2 });
  });

  data.contacts.forEach(c => {
    nodes.add({ id: c.resourceName, label: c.name, type: 'person',
      color: { background: '#111a2e', border: '#5b8dee', highlight: { background: '#162240', border: '#5b8dee' } },
      font: { color: '#e2e8f8', size: 12, face: 'DM Sans' }, shape: 'dot', size: 14, borderWidth: 1.5 });
    c.groups.forEach(grn => {
      if (allGroups[grn]) edges.add({ from: c.resourceName, to: grn,
        color: { color: '#252a38', highlight: '#5b8dee', opacity: 0.6 }, width: 1.5, smooth: { type: 'continuous' } });
    });
  });

  network = new vis.Network(document.getElementById('graph'), { nodes, edges }, {
    physics: { stabilization: { iterations: 150 }, barnesHut: { gravitationalConstant: -8000, springLength: 120, springConstant: 0.04 } },
    interaction: { hover: true },
  });

  network.on('click', params => {
    if (!params.nodes.length) return;
    const contact = allContacts[params.nodes[0]];
    if (contact) openPanel(contact);
  });

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
  body.appendChild(textField('Birthday', 'birthday', contact.birthday||'', 'YYYY-MM-DD'));
  body.appendChild(textareaField('Notes / Bio', 'bio', contact.bio||''));

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
  g.appendChild(l); g.appendChild(i); return g;
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

function relationRow(r) {
  const item = document.createElement('div'); item.className = 'list-item';
  const removeBtn = document.createElement('button'); removeBtn.className = 'remove-btn'; removeBtn.textContent = '×';
  removeBtn.onclick = () => item.remove();
  item.appendChild(removeBtn);

  const row = document.createElement('div'); row.className = 'list-item-row';
  const nameInp = document.createElement('input'); nameInp.type = 'text'; nameInp.placeholder = 'Name'; nameInp.value = r.name||''; nameInp.dataset.relField = 'name';
  const sel = document.createElement('select'); sel.dataset.relField = 'type';
  RELATION_TYPES.forEach(t => { const o = document.createElement('option'); o.value = t; o.textContent = t; if (t === (r.type||'other')) o.selected = true; sel.appendChild(o); });
  row.appendChild(nameInp); row.appendChild(sel); item.appendChild(row);
  return item;
}

// ── Collect & save ────────────────────────────────────────────────────────────
function closePanel() { document.getElementById('panel').classList.remove('open'); currentContact = null; }

async function saveContact() {
  if (!currentContact) return;
  const btn = document.getElementById('save-btn');
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
      msg.textContent = '✓ Saved to Google Contacts';
      msg.className = 'status-msg success';
      Object.assign(allContacts[currentContact.resourceName], payload);
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

let searchIndex = -1; // currently highlighted result

function selectResult(item) {
  network.focus(item.dataset.rn, { scale: 1.5, animation: { duration: 500, easingFunction: 'easeInOutQuad' } });
  network.selectNodes([item.dataset.rn]);
  openPanel(allContacts[item.dataset.rn]);
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
  const matches = Object.values(allContacts).filter(c =>
    matchesWordStart(c.name) ||
    (c.nicknames||[]).some(n => matchesWordStart(n)) ||
    (c.emails||[]).some(e => matchesWordStart(e)) ||
    (c.phones||[]).some(p => p.number.includes(q))
  ).slice(0, 12);

  if (!matches.length) {
    searchResults.innerHTML = '<div class="search-empty">No contacts found</div>';
    searchResults.style.display = 'block';
    return;
  }

  matches.forEach(c => {
    const item = document.createElement('div');
    item.className = 'search-result';
    item.dataset.rn = c.resourceName;
    const sub = c.emails[0] || (c.phones[0] && c.phones[0].number) || (c.groupNames||[]).join(', ') || '';
    item.innerHTML = `<span class="search-result-name">${highlight(c.name, query)}</span>
                      <span class="search-result-sub">${highlight(sub, query)}</span>`;
    item.onclick = () => selectResult(item);
    searchResults.appendChild(item);
  });
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
    if (searchIndex >= 0 && items[searchIndex]) {
      selectResult(items[searchIndex]);
    } else if (items.length === 1) {
      selectResult(items[0]);
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
