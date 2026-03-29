# Contact Map

An interactive graph visualization of your Google Contacts and groups. View, explore, and edit your contacts directly from a graph interface that runs locally on your machine.

---

## Features

- Interactive graph with person and group nodes
- Click any contact to open an edit panel
- Edit name, nickname, emails, phones, birthday, addresses, relationships, groups, and notes
- Create and delete contact groups
- Search contacts and groups with keyboard navigation
- Relationship edges shown as directed yellow dotted lines
- Automatically syncs changes back to Google Contacts

---

## Requirements

- Python 3.10+
- A Google account
- Google Cloud project with the People API enabled

---

## Setup

### 1. Google Cloud Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project
3. Go to **APIs & Services → Enable APIs** and enable the **People API**
4. Go to **APIs & Services → OAuth consent screen**
   - Set User Type to **External**
   - Add your Google email as a **Test User** under the **Audience** section
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Set Application type to **Desktop app**
   - Download the JSON file and rename it `credentials.json`
6. Place `credentials.json` in the project folder

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Run

```bash
python graph.py
```

On first run, a browser window will open asking you to log in and approve access. After that, a `token.json` file is saved so you won't need to log in again.

The app opens automatically at `http://localhost:5000`. Press `Ctrl+C` to stop the server.

---

## Important: Use Google Contacts Only

**This app syncs exclusively with Google Contacts.** To ensure everything works correctly, all contact and group management should be done through:

- The **Contact Map app** itself
- [contacts.google.com](https://contacts.google.com)

### Do NOT use Samsung Contacts or other Android contact apps to manage groups

Samsung Contacts and other Android contact apps create their own internal groups that are stored as `SYSTEM_CONTACT_GROUP` types on Google's servers. These groups behave differently from user-created groups (`USER_CONTACT_GROUP`) and have the following issues:

- **They cannot be modified via the API** — Google blocks add/remove operations on them with a "Cannot modify a system contact group" error
- **They cannot be deleted via the API** — attempts to delete them will fail
- **They create duplicates** — if you already have a "Family" group created in Google Contacts, Samsung may create a separate lowercase "family" group that appears as a duplicate

**The safest workflow is:**
1. Create groups using the **+ Create** button in the app's group panel
2. Assign contacts to groups using the checkboxes in the edit panel
3. Manage contacts at [contacts.google.com](https://contacts.google.com) if you prefer a web interface

---

## File Structure

```
Contact_Map/
├── graph.py              # Flask server, Google API auth, all API routes
├── fetch_contacts.py     # Standalone script to fetch and debug contact data
├── static/
│   └── app.js            # All frontend JavaScript
├── templates/
│   └── index.html        # HTML structure and CSS styles
├── .gitignore
└── README.md
```

---

## Security

- `credentials.json` and `token.json` are listed in `.gitignore` and should **never be committed to version control**
- The app only runs locally — no data is sent anywhere except directly to Google's API
- Access can be revoked at any time from your [Google Account permissions page](https://myaccount.google.com/permissions)
