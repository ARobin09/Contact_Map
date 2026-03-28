import os
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read+Write scope so we can edit contacts later
SCOPES = ["https://www.googleapis.com/auth/contacts"]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# All available People API fields
PERSON_FIELDS = ",".join([
    "names",
    "emailAddresses",
    "phoneNumbers",
    "birthdays",
    "addresses",
    "organizations",
    "relations",
    "urls",
    "biographies",
    "nicknames",
    "occupations",
    "interests",
    "skills",
    "braggingRights",
    "imClients",
    "events",
    "sipAddresses",
    "memberships",
    "metadata",
    "photos",
    "genders",
    "ageRanges",
    "locales",
    "residences",
    "externalIds",
    "userDefined",
])


def authenticate():
    """
    Handles OAuth2 login. Opens a browser window on first run,
    then caches the session in token.json for future runs.
    """
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


def fetch_contact_groups(service):
    """
    Fetches all contact groups (labels) from your Google account.
    Returns a dict: { groupResourceName -> groupName }
    """
    groups = {}
    result = service.contactGroups().list().execute()

    for group in result.get("contactGroups", []):
        if group.get("groupType") == "USER_CONTACT_GROUP":
            groups[group["resourceName"]] = group["name"]

    return groups


def parse_contact(person, groups):
    """
    Extracts all available fields from a People API person object.
    """
    def first(lst, *keys):
        """Safely get the first item's value by chained keys."""
        if not lst:
            return None
        val = lst[0]
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return None
        return val

    # Name
    names = person.get("names", [])
    name = first(names, "displayName") or "Unnamed"

    # Emails
    emails = [e.get("value") for e in person.get("emailAddresses", []) if e.get("value")]

    # Phone numbers
    phones = [
        {"number": p.get("value"), "type": p.get("type", "other")}
        for p in person.get("phoneNumbers", [])
        if p.get("value")
    ]

    # Birthday
    bday = person.get("birthdays", [{}])[0].get("date") if person.get("birthdays") else None
    birthday = None
    if bday:
        birthday = f"{bday.get('year', '????')}-{bday.get('month', '??'):02d}-{bday.get('day', '??'):02d}" \
            if isinstance(bday.get('month'), int) else None

    # Addresses
    addresses = [
        {
            "formatted": a.get("formattedValue"),
            "type": a.get("type", "other"),
            "street": a.get("streetAddress"),
            "city": a.get("city"),
            "region": a.get("region"),
            "country": a.get("country"),
            "postalCode": a.get("postalCode"),
        }
        for a in person.get("addresses", [])
    ]

    # Organization / Job
    orgs = [
        {
            "name": o.get("name"),
            "title": o.get("title"),
            "department": o.get("department"),
            "type": o.get("type"),
        }
        for o in person.get("organizations", [])
    ]

    # Relationships (e.g. spouse, child, friend)
    relations = [
        {"name": r.get("person"), "type": r.get("type", r.get("formattedType", "other"))}
        for r in person.get("relations", [])
    ]

    # Websites / URLs
    urls = [{"url": u.get("value"), "type": u.get("type", "other")} for u in person.get("urls", [])]

    # Bio / Notes
    bio = first(person.get("biographies", []), "value")

    # Nicknames
    nicknames = [n.get("value") for n in person.get("nicknames", []) if n.get("value")]

    # Gender
    gender = first(person.get("genders", []), "value")

    # Custom events (e.g. anniversary)
    events = [
        {
            "type": e.get("type", e.get("formattedType", "other")),
            "date": e.get("date"),
        }
        for e in person.get("events", [])
    ]

    # IM clients (e.g. Skype, AIM)
    im_clients = [
        {"username": i.get("username"), "protocol": i.get("protocol", i.get("formattedProtocol", ""))}
        for i in person.get("imClients", [])
    ]

    # User-defined custom fields
    custom_fields = [
        {"key": u.get("key"), "value": u.get("value")}
        for u in person.get("userDefined", [])
    ]

    # External IDs
    external_ids = [
        {"type": e.get("type"), "value": e.get("value")}
        for e in person.get("externalIds", [])
    ]

    # Group memberships
    memberships = person.get("memberships", [])
    group_resource_names = [
        m["contactGroupMembership"]["contactGroupResourceName"]
        for m in memberships
        if "contactGroupMembership" in m
    ]
    group_names = [groups.get(g, g) for g in group_resource_names]

    return {
        "resourceName": person.get("resourceName"),
        "etag": person.get("etag", ""),
        "name": name,
        "emails": emails,
        "phones": phones,
        "birthday": birthday,
        "gender": gender,
        "addresses": addresses,
        "organizations": orgs,
        "relations": relations,
        "urls": urls,
        "bio": bio,
        "nicknames": nicknames,
        "events": events,
        "imClients": im_clients,
        "customFields": custom_fields,
        "externalIds": external_ids,
        "groups": group_resource_names,
        "groupNames": group_names,
    }


def fetch_contacts(service, groups):
    """
    Fetches all contacts with all available fields.
    """
    contacts = []
    next_page_token = None

    while True:
        result = service.people().connections().list(
            resourceName="people/me",
            pageSize=1000,
            personFields=PERSON_FIELDS,
            pageToken=next_page_token,
        ).execute()

        for person in result.get("connections", []):
            contacts.append(parse_contact(person, groups))

        next_page_token = result.get("nextPageToken")
        if not next_page_token:
            break

    return contacts


def main():
    print("Authenticating...")
    creds = authenticate()
    service = build("people", "v1", credentials=creds)
    print("✓ Authenticated\n")

    print("Fetching contact groups...")
    groups = fetch_contact_groups(service)
    print(f"✓ Found {len(groups)} custom group(s):")
    for name in groups.values():
        print(f"   - {name}")
    print()

    print("Fetching contacts (with all fields)...")
    contacts = fetch_contacts(service, groups)
    print(f"✓ Found {len(contacts)} contact(s)\n")

    # Preview a few contacts
    print("Sample contacts:")
    print("-" * 40)
    for c in contacts[:5]:
        print(f"  Name:      {c['name']}")
        print(f"  Emails:    {', '.join(c['emails']) or 'N/A'}")
        print(f"  Phone:     {', '.join(p['number'] for p in c['phones']) or 'N/A'}")
        print(f"  Birthday:  {c['birthday'] or 'N/A'}")
        print(f"  Groups:    {', '.join(c['groupNames']) or 'None'}")
        print(f"  Relations: {', '.join(r['name'] for r in c['relations']) or 'None'}")
        print(f"  Org:       {c['organizations'][0]['name'] if c['organizations'] else 'N/A'}")
        print()

    # Save everything
    output = {"groups": groups, "contacts": contacts}
    with open("contacts_data.json", "w") as f:
        json.dump(output, f, indent=2)
    print("✓ Full data saved to contacts_data.json")


if __name__ == "__main__":
    main()
