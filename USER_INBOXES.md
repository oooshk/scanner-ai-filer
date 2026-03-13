# Multi-User Inbox System

The scanner system now supports individual Samba shares for different people, allowing documents to be filed to user-specific folders automatically.

## Overview

Instead of a single `scanner_inbox` share where all documents go to the same archive, you can now set up individual inboxes:

- `scanner/rob_inbox` → files filed to `Home Filing/Rob/archive/...`
- `scanner/john_inbox` → files filed to `Home Filing/John/archive/...`
- `scanner/jane_inbox` → files filed to `Home Filing/Jane/archive/...`

Each user's documents are automatically routed to their own folder structure.

## Setup

### 1. Access the Setup Modal

1. Log in to the web UI at `http://192.168.1.x:8090`
2. Click the **Setup** button in the top right
3. Open the **User Inboxes** section

### 2. Add User Inboxes

1. Enter the **Username** (e.g., `rob`, `john`)
   - Must be 3-64 characters
   - Can contain letters, numbers, dots (.), underscores (_), and hyphens (-)
   - Must start with a letter

2. Enter the **NAS path** (e.g., `Home Filing/Rob`, `Home Filing/John`)
   - This is where the user's documents will be stored on the NAS

3. Click **Add Inbox** to create the configuration

The system will automatically create a Samba share named `scanner_{username}` (e.g., `scanner_rob`, `scanner_john`)

### 3. Apply Samba Shares

Once you've added all user inboxes:

1. In the **Apply Samba Shares** section, enter the **Samba password** for the scanner shares
2. Click **Apply User Inboxes**

This will create the actual Samba shares on your Raspberry Pi. Each share maps to its own folder:
- `scanner_rob` → `/home/pi/scanner/inbox/rob`
- `scanner_john` → `/home/pi/scanner/inbox/john`

### 4. Manage User Inboxes

The User Inboxes section displays a table of all configured inboxes:

| Username | Samba Share | NAS Path | Action |
|----------|-------------|----------|--------|
| rob      | scanner_rob | Home Filing/Rob | [Delete] |
| john     | scanner_john | Home Filing/John | [Delete] |

To remove a user inbox, click **Delete**. The Samba share will need to be removed separately if desired.

## How It Works

### Document Routing

When a PDF is scanned:

1. **Source Detection**: System detects which inbox it came from based on the folder structure:
   - `inbox/rob/document.pdf` → user is `rob`
   - `inbox/john/document.pdf` → user is `john`
   - `inbox/document.pdf` → no specific user (general inbox)

2. **Classification**: The LLM classifies the document type and identifies the sender

3. **Filing**: Document is filed to the appropriate location:
   - **For user inboxes**: `archive/{username}/{doc_type}/{year}/{sender}/document.pdf`
     - Example: `archive/rob/Insurance/2025/Axa/policy_123.pdf`
   - **For review** (low confidence): `review/{username}/document.pdf`
   - **For rejected** (processing failed): `rejected/{username}/document.pdf`

4. **General inbox** (inbox root): Files to the standard location
   - `archive/{doc_type}/{year}/{sender}/document.pdf`
   - `review/document.pdf`
   - `rejected/document.pdf`

### Storage Structure

If your NAS is mounted at `/mnt/nas/Home Filing/`:

```
/mnt/nas/
└── Home Filing/
    ├── Rob/            # Rob's user-specific folder
    │   ├── archive/
    │   │   ├── Insurance/2025/Axa/...
    │   │   └── Utilities/2025/EDF/...
    │   ├── review/
    │   └── rejected/
    ├── John/           # John's user-specific folder
    │   ├── archive/
    │   ├── review/
    │   └── rejected/
    ├── archive/        # General inbox documents
    ├── review/
    └── rejected/
```

### Samba Share Credentials

Each user inbox share uses the same Samba password, but with a different username. For example:

- Share: `scanner_rob` @ `\\pi.local\scanner_rob`  
  User: `rob` / Password: (your samba password)

- Share: `scanner_john` @ `\\pi.local\scanner_john`  
  User: `john` / Password: (your samba password)

## Scanner Configuration

In your scanner application, configure it to use the appropriate Samba share:

- **Scanner for Rob**: Connect to `\\pi.local\scanner_rob` with username `rob`
- **Scanner for John**: Connect to `\\pi.local\scanner_john` with username `john`

## Backup & Restore

The user inbox configuration is automatically included in backups:
- Configuration: `state/user_inboxes.json`
- Inbox directories: `runtime/inbox/`
- User-specific archives: Included when backing up documents

## Troubleshooting

### Samba Share Not Accessible

1. Verify the share exists: `sudo smbstatus` or `sudo net share`
2. Check Pi's hostname: `hostname` (usually `raspberrypi` or similar)
3. Ensure password is correct
4. Verify Pi is accessible on network: `ping pi.local`

### Documents Not Filing to Correct Location

1. Check inbox structure: `ls -la /home/pi/scanner/inbox/`
2. Verify username is correctly configured in the setup
3. Check that username matches the Samba share folder
4. Review processing logs: `tail -f /tmp/scanner_filer.log`

### Missing Review/Rejected Folders

The system creates these folders automatically when needed. If documents aren't reaching them:

1. Check file permissions: `ls -la /mnt/nas/'Home Filing'/`
2. Ensure the archive parent path is correct
3. Manually create the folders if needed:
   ```bash
   mkdir -p /mnt/nas/'Home Filing'/{rob,john}/{archive,review,rejected}
   chmod 755 /mnt/nas/'Home Filing'/{rob,john}
   ```

## Examples

### Setting Up for Three People

1. **Add Rob's inbox**:
   - Username: `rob`
   - NAS path: `Home Filing/Rob`

2. **Add John's inbox**:
   - Username: `john`
   - NAS path: `Home Filing/John`

3. **Add Jane's inbox**:
   - Username: `jane`
   - NAS path: `Home Filing/Jane`

4. **Apply Samba Shares** with password: `YourSecurePassword123!`

This creates three Samba shares:
- `scanner_rob` → `/home/pi/scanner/inbox/rob/` → `Home Filing/Rob/`
- `scanner_john` → `/home/pi/scanner/inbox/john/` → `Home Filing/John/`
- `scanner_jane` → `/home/pi/scanner/inbox/jane/` → `Home Filing/Jane/`

### Scanner Workflow

1. Configure your office scanner to use `\\pi.local\scanner_rob`
2. Scan documents (invoices, statements, etc.)
3. Documents are temporarily stored in `/home/pi/scanner/inbox/rob/`
4. Processing pipeline:
   - Extracts text via OCR
   - Classifies document type (invoice, statement, etc.)
   - Files to `Rob/archive/Invoices/2025/Vendor Name/`
   - Moves to `Rob/review/` if confidence is low
   - Moves to `Rob/rejected/` if processing fails
5. Documents are accessible on NAS at `Home Filing/Rob/`

## Advanced: Merging Users Later

If you need to consolidate documents from multiple users:

1. The documents are organized as: `archive/{username}/...`
2. You can manually move/merge them: `mv /mnt/nas/'Home Filing'/Rob/*/* /mnt/nas/'Home Filing'/Combined/`
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
    "rob": {
      "samba_share": "scanner_rob",
      "nas_path": "Home Filing/Rob",
      "enabled": true
    },
    "john": {
      "samba_share": "scanner_john",
      "nas_path": "Home Filing/John",
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
