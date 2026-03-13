# Multi-User Inbox System

The scanner system now supports individual Samba shares for different people, allowing documents to be filed to user-specific folders automatically.

## Overview

Instead of a single `scanner_inbox` share where all documents go to the same archive, you can now set up individual inboxes:

- `scanner/user_a_inbox` -> files filed to `Filing/UserA/archive/...`
- `scanner/user_b_inbox` -> files filed to `Filing/UserB/archive/...`
- `scanner/user_c_inbox` -> files filed to `Filing/UserC/archive/...`

Each user's documents are automatically routed to their own folder structure.

## Setup

### 1. Access the Setup Modal

1. Log in to the web UI at `http://<SCANNER_HOST_OR_IP>:8090`
2. Click the **Setup** button in the top right
3. Open the **User Inboxes** section

### 2. Add User Inboxes

1. Enter the **Username** (e.g., `user_a`, `user_b`)
   - Must be 3-64 characters
   - Can contain letters, numbers, dots (.), underscores (_), and hyphens (-)
   - Must start with a letter

2. Enter the **NAS path** (e.g., `Filing/UserA`, `Filing/UserB`)
   - This is where the user's documents will be stored on the NAS

3. Click **Add Inbox** to create the configuration

The system will automatically create a Samba share named `scanner_{username}` (e.g., `scanner_user_a`, `scanner_user_b`)

### 3. Apply Samba Shares

Once you've added all user inboxes:

1. In the **Apply Samba Shares** section, enter the **Samba password** for the scanner shares
2. Click **Apply User Inboxes**

This will create the actual Samba shares on your host machine. Each share maps to its own folder:
- `scanner_user_a` -> `<SCANNER_DIR>/inbox/user_a`
- `scanner_user_b` -> `<SCANNER_DIR>/inbox/user_b`

### 4. Manage User Inboxes

The User Inboxes section displays a table of all configured inboxes:

| Username | Samba Share | NAS Path | Action |
|----------|-------------|----------|--------|
| user_a   | scanner_user_a | Filing/UserA | [Delete] |
| user_b   | scanner_user_b | Filing/UserB | [Delete] |

To remove a user inbox, click **Delete**. The Samba share will need to be removed separately if desired.

## How It Works

### Document Routing

When a PDF is scanned:

1. **Source Detection**: System detects which inbox it came from based on the folder structure:
   - `inbox/user_a/document.pdf` -> user is `user_a`
   - `inbox/user_b/document.pdf` -> user is `user_b`
   - `inbox/document.pdf` → no specific user (general inbox)

2. **Classification**: The LLM classifies the document type and identifies the sender

3. **Filing**: Document is filed to the appropriate location:
   - **For user inboxes**: `archive/{username}/{doc_type}/{year}/{sender}/document.pdf`
   - Example: `archive/user_a/Insurance/2025/Provider/policy_123.pdf`
   - **For review** (low confidence): `review/{username}/document.pdf`
   - **For rejected** (processing failed): `rejected/{username}/document.pdf`

4. **General inbox** (inbox root): Files to the standard location
   - `archive/{doc_type}/{year}/{sender}/document.pdf`
   - `review/document.pdf`
   - `rejected/document.pdf`

### Storage Structure

If your NAS is mounted at `/mnt/nas/Filing/`:

```
/mnt/nas/
└── Filing/
   ├── UserA/          # UserA's user-specific folder
    │   ├── archive/
    │   │   ├── Insurance/2025/Axa/...
    │   │   └── Utilities/2025/EDF/...
    │   ├── review/
    │   └── rejected/
   ├── UserB/          # UserB's user-specific folder
    │   ├── archive/
    │   ├── review/
    │   └── rejected/
    ├── archive/        # General inbox documents
    ├── review/
    └── rejected/
```

### Samba Share Credentials

Each user inbox share uses the same Samba password, but with a different username. For example:

- Share: `scanner_user_a` @ `\\<SCANNER_HOST>\scanner_user_a`  
   User: `user_a` / Password: (set during setup)

- Share: `scanner_user_b` @ `\\<SCANNER_HOST>\scanner_user_b`  
   User: `user_b` / Password: (set during setup)

## Scanner Configuration

In your scanner application, configure it to use the appropriate Samba share:

- **Scanner for UserA**: Connect to `\\<SCANNER_HOST>\scanner_user_a` with username `user_a`
- **Scanner for UserB**: Connect to `\\<SCANNER_HOST>\scanner_user_b` with username `user_b`

## Backup & Restore

The user inbox configuration is automatically included in backups:
- Configuration: `state/user_inboxes.json`
- Inbox directories: `runtime/inbox/`
- User-specific archives: Included when backing up documents

## Troubleshooting

### Samba Share Not Accessible

1. Verify the share exists: `sudo smbstatus` or `sudo net share`
2. Check scanner host name: `hostname` (or use the host IP)
3. Ensure password is correct
4. Verify scanner host is accessible on network: `ping <SCANNER_HOST>`

### Documents Not Filing to Correct Location

1. Check inbox structure: `ls -la <SCANNER_DIR>/inbox/`
2. Verify username is correctly configured in the setup
3. Check that username matches the Samba share folder
4. Review processing logs: `tail -f /tmp/scanner_filer.log`

### Missing Review/Rejected Folders

The system creates these folders automatically when needed. If documents aren't reaching them:

1. Check file permissions: `ls -la /mnt/nas/'Home Filing'/`
2. Ensure the archive parent path is correct
3. Manually create the folders if needed:
   ```bash
   mkdir -p /mnt/nas/Filing/{user_a,user_b}/{archive,review,rejected}
   chmod 755 /mnt/nas/Filing/{user_a,user_b}
   ```

## Examples

### Setting Up for Three People

1. **Add UserA inbox**:
   - Username: `user_a`
   - NAS path: `Filing/UserA`

2. **Add UserB inbox**:
   - Username: `user_b`
   - NAS path: `Filing/UserB`

3. **Add UserC inbox**:
   - Username: `user_c`
   - NAS path: `Filing/UserC`

4. **Apply Samba Shares** with password: `<samba_share_password>`

This creates three Samba shares:
- `scanner_user_a` -> `<SCANNER_DIR>/inbox/user_a/` -> `Filing/UserA/`
- `scanner_user_b` -> `<SCANNER_DIR>/inbox/user_b/` -> `Filing/UserB/`
- `scanner_user_c` -> `<SCANNER_DIR>/inbox/user_c/` -> `Filing/UserC/`

### Scanner Workflow

1. Configure your office scanner to use `\\<SCANNER_HOST>\scanner_user_a`
2. Scan documents (invoices, statements, etc.)
3. Documents are temporarily stored in `<SCANNER_DIR>/inbox/user_a/`
4. Processing pipeline:
   - Extracts text via OCR
   - Classifies document type (invoice, statement, etc.)
   - Files to `UserA/archive/Invoices/2025/Vendor Name/`
   - Moves to `UserA/review/` if confidence is low
   - Moves to `UserA/rejected/` if processing fails
5. Documents are accessible on NAS at `Filing/UserA/`

## Advanced: Merging Users Later

If you need to consolidate documents from multiple users:

1. The documents are organized as: `archive/{username}/...`
2. You can manually move/merge them: `mv /mnt/nas/Filing/UserA/*/* /mnt/nas/Filing/Combined/`
3. Or keep them separate for organization purposes

## API Endpoints

The system provides REST endpoints for programmatic access:

### List User Inboxes
```
GET /api/user-inboxes
```
Returns:
```json
{
  "inboxes": {
      "user_a": {
         "samba_share": "scanner_user_a",
         "nas_path": "Filing/UserA",
      "enabled": true
    },
      "user_b": {
         "samba_share": "scanner_user_b",
         "nas_path": "Filing/UserB",
      "enabled": true
    }
  }
}
```

### Add User Inbox
```
POST /setup/user-inbox/add
```
Parameters:
- `name`: Username (3-64 chars, letter to start)
- `nas_path`: NAS folder path

### Delete User Inbox
```
POST /setup/user-inbox/delete
```
Parameters:
- `name`: Username to delete

### Apply Samba Shares
```
POST /setup/user-inbox/apply
```
Parameters:
- `samba_password`: Password for Samba shares

---

For questions or issues, check the main [README.md](README.md) or review the processing logs.
