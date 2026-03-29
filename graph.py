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

def fetch_groups():
    groups = {}
    result = service.contactGroups().list().execute()
    for g in result.get("contactGroups", []):
        if g.get("groupType") == "USER_CONTACT_GROUP":
            groups[g["resourceName"]] = g["name"]
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
    addresses  = [{"formatted": a.get("formattedValue"), "type": a.get("type","other")} for a in person.get("addresses",[])]
    orgs       = [{"name": o.get("name"), "title": o.get("title"), "department": o.get("department")} for o in person.get("organizations",[])]
    relations  = [{"name": r.get("person"), "type": r.get("type", r.get("formattedType","other"))} for r in person.get("relations",[])]
    urls       = [{"url": u.get("value"), "type": u.get("type","other")} for u in person.get("urls",[])]
    bio        = first(person.get("biographies",[]), "value")
    nicknames  = [n["value"] for n in person.get("nicknames",[]) if n.get("value")]
    gender     = first(person.get("genders",[]), "value")
    events     = [{"type": e.get("type", e.get("formattedType","other")), "date": e.get("date")} for e in person.get("events",[])]
    im_clients = [{"username": i.get("username"), "protocol": i.get("protocol", i.get("formattedProtocol",""))} for i in person.get("imClients",[])]
    custom     = [{"key": u.get("key"), "value": u.get("value")} for u in person.get("userDefined",[])]
    group_rns  = [m["contactGroupMembership"]["contactGroupResourceName"] for m in person.get("memberships",[]) if "contactGroupMembership" in m]
    group_names= [groups.get(g, g) for g in group_rns]

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
    return jsonify({"groups": groups, "contacts": contacts})

@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.json
    rn   = data.get("resourceName")
    if not rn:
        return jsonify({"error": "Missing resourceName"}), 400

    # Always re-fetch etag before updating
    person = service.people().get(resourceName=rn, personFields=PERSON_FIELDS).execute()
    etag   = person.get("etag", "")

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
    if "birthday" in data and data["birthday"]:
        parts = data["birthday"].split("-")
        if len(parts) == 3:
            body["birthdays"] = [{"date": {"year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])}}]
            fields.append("birthdays")

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    updated = service.people().updateContact(
        resourceName=rn,
        updatePersonFields=",".join(fields),
        body=body,
    ).execute()

    return jsonify({"success": True, "resourceName": updated.get("resourceName")})


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
    --node-person: #5b8dee;
    --node-group: #e85b8d;
    --success: #3ecf8e;
    --danger: #e85b5b;
  }

  body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }

  header h1 {
    font-family: 'DM Mono', monospace;
    font-size: 15px;
    font-weight: 500;
    letter-spacing: 0.05em;
    color: var(--text);
  }

  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .legend {
    display: flex;
    gap: 16px;
    margin-left: auto;
    font-size: 12px;
    color: var(--muted);
    font-family: 'DM Mono', monospace;
  }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }

  .main {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  #graph {
    flex: 1;
    background: var(--bg);
  }

  /* Side panel */
  #panel {
    width: 340px;
    background: var(--surface);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    transform: translateX(100%);
    transition: transform 0.3s cubic-bezier(0.4,0,0.2,1);
    flex-shrink: 0;
  }
  #panel.open { transform: translateX(0); }

  .panel-header {
    padding: 20px 20px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .panel-header h2 {
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  #close-panel {
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 18px;
    line-height: 1;
    padding: 2px 6px;
    border-radius: 4px;
    transition: color 0.2s, background 0.2s;
  }
  #close-panel:hover { color: var(--text); background: var(--surface2); }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .panel-body::-webkit-scrollbar { width: 4px; }
  .panel-body::-webkit-scrollbar-track { background: transparent; }
  .panel-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .field-group { display: flex; flex-direction: column; gap: 6px; }
  .field-label {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .field-value {
    font-size: 13px;
    color: var(--text);
    line-height: 1.5;
  }
  .field-value.muted { color: var(--muted); font-style: italic; }

  input[type="text"], input[type="date"], textarea {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    padding: 8px 10px;
    outline: none;
    transition: border-color 0.2s;
    resize: vertical;
  }
  input:focus, textarea:focus { border-color: var(--accent); }

  .tag-list { display: flex; flex-wrap: wrap; gap: 6px; }
  .tag {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 3px 10px;
    font-size: 12px;
    color: var(--muted);
    font-family: 'DM Mono', monospace;
  }
  .tag.group { border-color: var(--accent2); color: var(--accent2); }

  .divider {
    height: 1px;
    background: var(--border);
    margin: 4px 0;
  }

  .panel-footer {
    padding: 16px 20px;
    border-top: 1px solid var(--border);
    display: flex;
    gap: 8px;
  }
  button {
    flex: 1;
    padding: 9px 14px;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: opacity 0.2s, transform 0.1s;
  }
  button:active { transform: scale(0.98); }
  #save-btn {
    background: var(--accent);
    color: #fff;
  }
  #save-btn:hover { opacity: 0.85; }
  #save-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #cancel-btn {
    background: var(--surface2);
    color: var(--muted);
    border: 1px solid var(--border);
  }
  #cancel-btn:hover { color: var(--text); }

  .status-msg {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    text-align: center;
    padding: 6px;
    border-radius: 4px;
    display: none;
  }
  .status-msg.success { background: rgba(62,207,142,0.12); color: var(--success); display: block; }
  .status-msg.error   { background: rgba(232,91,91,0.12);  color: var(--danger);  display: block; }

  #loading {
    position: fixed; inset: 0;
    background: var(--bg);
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 16px; z-index: 999;
  }
  .spinner {
    width: 36px; height: 36px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #loading p { font-family: 'DM Mono', monospace; font-size: 13px; color: var(--muted); }
</style>
</head>
<body>

<div id="loading">
  <div class="spinner"></div>
  <p>loading contacts...</p>
</div>

<header>
  <div class="dot"></div>
  <h1>contact_map</h1>
  <div class="legend">
    <div class="legend-item">
      <div class="legend-dot" style="background:var(--node-person)"></div>
      <span>person</span>
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:var(--node-group)"></div>
      <span>group</span>
    </div>
  </div>
</header>

<div class="main">
  <div id="graph"></div>

  <div id="panel">
    <div class="panel-header">
      <h2>Contact</h2>
      <button id="close-panel">×</button>
    </div>
    <div class="panel-body" id="panel-body"></div>
    <div class="panel-footer">
      <button id="cancel-btn">Cancel</button>
      <button id="save-btn">Save Changes</button>
    </div>
  </div>
</div>

<script>
const API = 'http://localhost:5000/api';
let network, allContacts = {}, currentContact = null;

// ── Load data & build graph ──────────────────────────────────────────────────
async function loadGraph() {
  const res  = await fetch(`${API}/data`);
  const data = await res.json();
  const { groups, contacts } = data;

  // Index contacts by resourceName for quick lookup
  contacts.forEach(c => allContacts[c.resourceName] = c);

  const nodes = new vis.DataSet();
  const edges = new vis.DataSet();

  // Group nodes
  Object.entries(groups).forEach(([rn, name]) => {
    nodes.add({
      id: rn, label: name, type: 'group',
      color: { background: '#1f1529', border: '#e85b8d', highlight: { background: '#2a1a38', border: '#e85b8d' } },
      font: { color: '#e85b8d', size: 13, face: 'DM Mono' },
      shape: 'dot', size: 22,
      borderWidth: 2,
    });
  });

  // Person nodes + edges
  contacts.forEach(c => {
    nodes.add({
      id: c.resourceName,
      label: c.name,
      type: 'person',
      color: { background: '#111a2e', border: '#5b8dee', highlight: { background: '#162240', border: '#5b8dee' } },
      font: { color: '#e2e8f8', size: 12, face: 'DM Sans' },
      shape: 'dot', size: 14,
      borderWidth: 1.5,
    });
    c.groups.forEach(grn => {
      if (groups[grn]) {
        edges.add({
          from: c.resourceName, to: grn,
          color: { color: '#252a38', highlight: '#5b8dee', opacity: 0.6 },
          width: 1.5, smooth: { type: 'continuous' },
        });
      }
    });
  });

  const container = document.getElementById('graph');
  network = new vis.Network(container, { nodes, edges }, {
    physics: {
      stabilization: { iterations: 150 },
      barnesHut: { gravitationalConstant: -8000, springLength: 120, springConstant: 0.04 },
    },
    interaction: { hover: true, tooltipDelay: 200 },
  });

  network.on('click', params => {
    if (!params.nodes.length) return;
    const id = params.nodes[0];
    const contact = allContacts[id];
    if (contact) openPanel(contact);
  });

  document.getElementById('loading').style.display = 'none';
}

// ── Panel ────────────────────────────────────────────────────────────────────
function openPanel(contact) {
  currentContact = contact;
  const body = document.getElementById('panel-body');
  body.innerHTML = '';

  const fields = [
    { label: 'Full Name', key: 'name', type: 'text', value: contact.name },
    { label: 'Email(s)', key: 'emails', type: 'emails', value: contact.emails },
    { label: 'Phone(s)', key: 'phones', type: 'phones', value: contact.phones },
    { label: 'Birthday', key: 'birthday', type: 'date', value: contact.birthday },
    { label: 'Notes / Bio', key: 'bio', type: 'textarea', value: contact.bio },
  ];

  fields.forEach(f => {
    const group = document.createElement('div');
    group.className = 'field-group';
    const label = document.createElement('div');
    label.className = 'field-label';
    label.textContent = f.label;
    group.appendChild(label);

    if (f.type === 'text') {
      const inp = document.createElement('input');
      inp.type = 'text'; inp.value = f.value || '';
      inp.dataset.key = f.key;
      group.appendChild(inp);
    } else if (f.type === 'date') {
      const inp = document.createElement('input');
      inp.type = 'text'; inp.placeholder = 'YYYY-MM-DD';
      inp.value = f.value || '';
      inp.dataset.key = f.key;
      group.appendChild(inp);
    } else if (f.type === 'textarea') {
      const ta = document.createElement('textarea');
      ta.rows = 3; ta.value = f.value || '';
      ta.dataset.key = f.key;
      group.appendChild(ta);
    } else if (f.type === 'emails') {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = (f.value || []).join(', ');
      inp.dataset.key = f.key;
      inp.placeholder = 'email1@x.com, email2@x.com';
      group.appendChild(inp);
    } else if (f.type === 'phones') {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = (f.value || []).map(p => p.number).join(', ');
      inp.dataset.key = f.key;
      inp.placeholder = '+1 555 000 0000, ...';
      group.appendChild(inp);
    }

    body.appendChild(group);
  });

  // Read-only info
  if (contact.organizations?.length) {
    body.appendChild(divider());
    const org = contact.organizations[0];
    body.appendChild(readOnly('Organization', [org.title, org.name, org.department].filter(Boolean).join(' · ')));
  }
  if (contact.relations?.length) {
    body.appendChild(readOnly('Relationships', contact.relations.map(r => `${r.name} (${r.type})`).join(', ')));
  }
  if (contact.groupNames?.length) {
    body.appendChild(divider());
    const g = document.createElement('div');
    g.className = 'field-group';
    const gl = document.createElement('div'); gl.className = 'field-label'; gl.textContent = 'Groups';
    const tags = document.createElement('div'); tags.className = 'tag-list';
    contact.groupNames.forEach(name => {
      const t = document.createElement('div'); t.className = 'tag group'; t.textContent = name;
      tags.appendChild(t);
    });
    g.appendChild(gl); g.appendChild(tags);
    body.appendChild(g);
  }
  if (contact.nicknames?.length) {
    body.appendChild(readOnly('Nicknames', contact.nicknames.join(', ')));
  }
  if (contact.addresses?.length) {
    body.appendChild(readOnly('Address', contact.addresses[0].formatted));
  }

  // Status msg
  const msg = document.createElement('div');
  msg.className = 'status-msg'; msg.id = 'status-msg';
  body.appendChild(msg);

  document.getElementById('panel').classList.add('open');
  document.getElementById('save-btn').disabled = false;
}

function divider() {
  const d = document.createElement('div'); d.className = 'divider'; return d;
}

function readOnly(label, value) {
  const g = document.createElement('div'); g.className = 'field-group';
  const l = document.createElement('div'); l.className = 'field-label'; l.textContent = label;
  const v = document.createElement('div'); v.className = 'field-value' + (value ? '' : ' muted');
  v.textContent = value || '—';
  g.appendChild(l); g.appendChild(v);
  return g;
}

function closePanel() {
  document.getElementById('panel').classList.remove('open');
  currentContact = null;
}

// ── Save ─────────────────────────────────────────────────────────────────────
async function saveContact() {
  if (!currentContact) return;
  const btn = document.getElementById('save-btn');
  btn.disabled = true; btn.textContent = 'Saving...';

  const body = { resourceName: currentContact.resourceName };

  document.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (key === 'emails') {
      body.emails = el.value.split(',').map(s => s.trim()).filter(Boolean);
    } else if (key === 'phones') {
      body.phones = el.value.split(',').map(s => ({ number: s.trim(), type: 'mobile' })).filter(p => p.number);
    } else {
      body[key] = el.value.trim();
    }
  });

  try {
    const res = await fetch(`${API}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    const msg = document.getElementById('status-msg');
    if (data.success) {
      msg.textContent = '✓ Saved to Google Contacts';
      msg.className = 'status-msg success';
      // Update local cache
      Object.assign(allContacts[currentContact.resourceName], body);
    } else {
      msg.textContent = data.error || 'Something went wrong';
      msg.className = 'status-msg error';
    }
  } catch (e) {
    const msg = document.getElementById('status-msg');
    msg.textContent = 'Could not reach local server';
    msg.className = 'status-msg error';
  }

  btn.disabled = false; btn.textContent = 'Save Changes';
}

document.getElementById('close-panel').addEventListener('click', closePanel);
document.getElementById('cancel-btn').addEventListener('click', closePanel);
document.getElementById('save-btn').addEventListener('click', saveContact);

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
