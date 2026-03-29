import os
import json
import threading
import webbrowser
from flask import Flask, request, jsonify, render_template
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



# ─── Serve frontend ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")



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
