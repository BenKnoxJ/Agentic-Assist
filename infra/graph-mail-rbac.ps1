# Scope Gojo's mailbox access to one mailbox — Exchange Online App RBAC.
#
# ⚠ Read this before running anything.
#
# GOJO-MASTER.md 8.3/8.4 mandate granting Mail.Read in Entra and then
# restricting it with New-ApplicationAccessPolicy. That mechanism is legacy.
# Microsoft: "New access configuration should not use Application Access
# Policies since this feature will have deprecation announced in the future."
#
# The replacement, RBAC for Applications, works the other way round and is
# strictly safer: Mail.Read is NEVER granted tenant-wide. The old sequence
# has a window - however brief - in which this app can read every mailbox in
# conversant.technology. This one has no such window.
#
# ⚠ Do NOT also grant Mail.Read in Entra. Microsoft's FAQ is explicit that
# Entra grants and RBAC grants are a UNION, so an unscoped Entra grant
# alongside a scoped RBAC grant results in no effective scoping at all.
#
# Requires: Exchange Administrator (Entra) or Organization Management
# (Exchange). Module: Install-Module ExchangeOnlineManagement
# Works in pwsh on Linux with device-code sign-in.

$AppId    = '2b6bad70-f62b-4148-9138-20c1d71319ca'   # Application (client) ID
$SpObject = 'ad389ad2-5beb-4738-aa36-92052bc365e8'   # Service principal Object ID
$Mailbox  = 'ben.knox-johnston@conversant.technology'
$ScopeName = 'Gojo-OwnerMailbox'

# Both IDs come from Enterprise applications, NOT App registrations - that
# page shows different values and using them produces a valid-looking but
# wrong assignment. Measured 7 Aug 2026: this file originally carried the
# App registration's Object ID and New-ServicePrincipal rejected it with
# AADServicePrincipalNotFound - a loud failure, but only because the ID was
# absent entirely; the warning above still matters. Fastest route to the
# right value: App registrations -> the app -> Overview -> "Managed
# application in local directory" link -> Object ID on THAT page.

Connect-ExchangeOnline

# 1. A pointer in Exchange to the Entra service principal. Exchange cannot
#    create service principals; this only references the existing one.
New-ServicePrincipal -AppId $AppId -ObjectId $SpObject -DisplayName 'Gojo'

# 2. The resource scope: exactly one mailbox.
New-ManagementScope -Name $ScopeName `
  -RecipientRestrictionFilter "PrimarySmtpAddress -eq '$Mailbox'"

# 3. The scoped grant. This is the only place Mail.Read is ever granted.
New-ManagementRoleAssignment -App $SpObject `
  -Role 'Application Mail.Read' -CustomResourceScope $ScopeName

# 4. Prove it. InScope must be True here...
Test-ServicePrincipalAuthorization -Identity 'Gojo' -Resource $Mailbox | Format-Table

# 5. ...and False here. The negative test is the one that proves scoping
#    works; step 4 passing alone would also pass with tenant-wide access.
#    Substitute any colleague's address.
# Test-ServicePrincipalAuthorization -Identity 'Gojo' -Resource 'someone.else@conversant.technology' | Format-Table

# Note: permission changes cache for 30 minutes to 2 hours. The Test cmdlet
# bypasses the cache, so trust it over live Graph calls when verifying.

# --- Step 5: the write roles (ADR 0011). Run only AFTER docs/THREAT-MODEL.md
# §4 has been re-argued for writes - its §7 makes that a precondition.
# Same scope object, same union hazard: nothing is EVER granted in Entra.

# 6. Drafts rung: create/update messages in the owner's mailbox only.
New-ManagementRoleAssignment -App $SpObject `
  -Role 'Application Mail.ReadWrite' -CustomResourceScope $ScopeName

# 7. Send rung: send as the owner's mailbox only.
New-ManagementRoleAssignment -App $SpObject `
  -Role 'Application Mail.Send' -CustomResourceScope $ScopeName

# 8. Prove BOTH, both ways. Positive (owner) must be True...
Test-ServicePrincipalAuthorization -Identity 'Gojo' -Resource $Mailbox | Format-Table

# 9. ...and the negative (a real colleague's mailbox - substitute one that
#    exists; "not found" proves nothing) must show False for every role.
# Test-ServicePrincipalAuthorization -Identity 'Gojo' -Resource 'someone.real@conversant.technology' | Format-Table
