# Azure ODCR Automation for SDAF

[![Azure](https://img.shields.io/badge/Azure-0078D4?style=flat&logo=microsoftazure&logoColor=white)](https://azure.microsoft.com)
[![SAP](https://img.shields.io/badge/SAP-0FAAFF?style=flat&logo=sap&logoColor=white)](https://www.sap.com)
[![Ansible](https://img.shields.io/badge/Ansible-EE0000?style=flat&logo=ansible&logoColor=white)](https://www.ansible.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> Role-based Azure On-Demand Capacity Reservation (ODCR) management for SAP systems deployed with the
> [SAP Deployment Automation Framework (SDAF)](https://github.com/Azure/sap-automation).

## Contents

- [Overview](#overview)
- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [Optional settings](#optional-settings)
- [Reading the output](#reading-the-output)
- [Common scenarios](#common-scenarios)
- [Cleanup: remove and redeploy](#cleanup-remove-and-redeploy)
- [Troubleshooting](#troubleshooting)
- [Limitations and notes](#limitations-and-notes)
- [Files](#files)
- [References](#references)

---

## Overview

The tool reserves Azure VM capacity for an SAP system (SID) and associates the SID's VMs with those reservations,
so the VMs can always be restarted or redeployed in their availability zone, even when the zone is short on capacity.

It is run once per SID from the SDAF deployer, after the SID's infrastructure is deployed. It is safe to re-run:
VMs that are already associated are skipped, and only missing capacity is added.

Key features:

- **Role-based placement**: App servers use a shared, central reservation pool per environment and region;
  SCS (ASCS/ERS) and database servers use a dedicated reservation group in the SID's own resource group.
- **Plan mode**: shows every change before anything is created.
- **Grows only what's needed**: uses free reserved capacity first, then grows or creates reservations.
- **Prod / non-prod sharing**: the central App server pool can be shared between the prod and non-prod
  subscriptions.
- **No downtime**: zonal VMs are associated while running.

---

## How it works

### Inputs

The tool targets one SID resource group per run. The playbook reads only `resource_group_name` and
`subscription_id` from the SDAF inventory (`<SID>_hosts.yaml`) in the SID workspace.

The script queries Azure directly with `az vm list` in that resource group and subscription, retrieving each
VM's ID, name, location, size, availability zone and current CRG association. The inventory's host list is **not**
used to select VMs: servers added outside SDAF are included automatically if they follow the role naming
convention below. `sap-parameters.yaml` is not read by this playbook.

The Azure region comes from the VMs, **not** the resource group's metadata location. All VMs in the selected
resource group must report the same region, including VMs whose roles are skipped. Empty groups, missing or
invalid VM data, failed discovery calls, and mixed VM regions stop the run with an error. Existing target CRGs
are checked for a matching region before any CRG creation, sharing, reservation or VM-association changes.
The region code in the resource-group name (for example `SCUS`) remains a naming convention, not an Azure
location lookup.

The SID resource group must follow the SDAF naming convention `<ENV>-<REGION>-<VNET>-<SID>`, for example
`PRD-SCUS-TFO01-PGY`. The environment code decides which central pool is used:

| Environment code | Tier |
|---|---|
| `PRD` | prod |
| `NRD` | non-prod |

Any other code stops the run unless `odcr_tier` is set (see [Optional settings](#optional-settings)).

### VM roles

The role is read from the VM name (`<SID-RG>_<sid><role>…`, e.g. `PRD-SCUS-TFO01-PGY_pgyapp01l0a25`):

| VM name contains | Role | Reserved in |
|---|---|---|
| `<sid>app…` | App server | Central App server CRG (shared by all SIDs of the tier/region) |
| `<sid>scs…` | SCS (ASCS / ERS) | SID CRG in the SID resource group |
| `<sid>d…` | Database | SID CRG in the SID resource group |
| `<sid>web…` | Web Dispatcher | Not reserved - reported only |

### Naming

Example for SID resource group `PRD-SCUS-TFO01-PGY`:

| Resource | Pattern | Example |
|---|---|---|
| Central resource group (pre-created, one per tier per region) | `<ENV>-<REGION>-CR` | `PRD-SCUS-CR` / `NRD-SCUS-CR` |
| Central App server CRG | `<REGION>-CR` | `SCUS-CR` |
| App server reservation | `<REGION>-AZ<zz>-Apps` | `SCUS-AZ01-Apps` |
| SID CRG | `<REGION>-<SID>-CR` | `SCUS-PGY-CR` |
| SCS reservation | `<REGION>-<SID>-AZ<zz>-ACS` | `SCUS-PGY-AZ01-ACS` |
| Database reservation | `<REGION>-<SID>-AZ<zz>-DB` | `SCUS-PGY-AZ03-DB` |

Azure allows one reservation per VM size per zone in a CRG, so existing reservations are found by VM size and zone,
not by name. If a new reservation's name is already used by a different VM size in the same CRG, the size is
appended, e.g. `SCUS-AZ01-Apps-E8ds_v5`.

### Decision logic

**App servers**

1. Check that the central resource group exists. If it doesn't, **stop** before making any change.
2. Create the central App server CRG in it if it doesn't exist yet.
3. For each app server:
   - Already associated with a CRG → skip.
   - Otherwise, find the reservation for its VM size and zone:
     - Free capacity available → associate.
     - Not enough free capacity → grow the reservation, then associate.
     - No reservation → create it, then associate.

**SCS and database servers**

1. Already associated with a CRG → skip.
2. VM size doesn't support capacity reservations → report it as not supported.
3. Otherwise, same as app servers, using the SID CRG (created if missing).

**Always**

- Web Dispatchers are skipped and reported.
- Regional (non-zonal) VMs are skipped and reported. Associating an existing regional VM requires a deallocation,
  which the tool never does ([Microsoft Learn](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-associate-vm)).

**Growth example**: a reservation has capacity 3 and 2 associated VMs (1 free). Two new app servers need a slot,
so the reservation grows by 1 to capacity 4, and both new VMs are associated. Capacity always ends up equal to the
number of associated VMs - every VM is covered and no unused capacity is added.

### Sharing between prod and non-prod

With [CRG sharing](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-group-share) (Azure
preview), the central App server CRG of one subscription can be used by the other:

- A prod run shares `PRD-<REGION>-CR / <REGION>-CR` with the non-prod subscription.
- A non-prod run shares `NRD-<REGION>-CR / <REGION>-CR` with the prod subscription.

Pass both subscription IDs on every run (`odcr_share_subscriptions`); the tool ignores the subscription it runs in.
Sharing is **add-only** - subscriptions are never removed from the sharing list. Sharing only makes the capacity
available to the other subscription; the tool always associates app servers with their own tier's pool. SCS and
database CRGs are not shared.

---

## Prerequisites

| Requirement | Details |
|---|---|
| SDAF deployer | The SID is deployed with SDAF and its workspace folder contains `<SID>_hosts.yaml` and `sap-parameters.yaml` |
| Tools on the deployer | Azure CLI (with `az capacity` commands), `jq`, bash 4+, Ansible (installed with SDAF) |
| Azure sign-in | The Azure CLI on the deployer is signed in (e.g. the deployer managed identity: `az login --identity`) |
| Central resource groups | `PRD-<REGION>-CR` and `NRD-<REGION>-CR` exist in the prod / non-prod subscriptions, in each region. The tool never creates them. |
| Naming | SID resource groups follow `<ENV>-<REGION>-<VNET>-<SID>` with `PRD` or `NRD` as the environment code |
| Zonal VMs | VMs are deployed in availability zones |
| Quota | Enough vCPU quota for the reserved capacity (reservations consume quota like running VMs) |

**Permissions** for the identity running the tool:

| Scope | Needed for |
|---|---|
| SID resource group | Read/update VMs, create and manage the SID CRG and its reservations |
| Central resource group | Create and manage the central CRG and its reservations; `Microsoft.Compute/capacityReservationGroups/share/action` for sharing |
| Subscription | `Microsoft.Compute/skus/read` (VM size support check) |

`Contributor` on both resource groups plus `Reader` on the subscription covers all of the above. When sharing,
the identity also needs these rights in the other subscription.

---

## Installation

The tool is two files that are copied into the SDAF repository on the deployer.

### 1. Copy the files

```bash
cd ~/Azure_SAP_Automated_Deployment
git clone https://github.com/DarylsCorner/azure-odcr-automation.git

cp azure-odcr-automation/playbook_11_00_00_capacity_reservations.yaml sap-automation/deploy/ansible/
cp azure-odcr-automation/odcr_management.sh sap-automation/deploy/scripts/
chmod +x sap-automation/deploy/scripts/odcr_management.sh
```

The playbook expects the script at `../scripts/odcr_management.sh` relative to itself, so keep these locations.

To update later: `git -C azure-odcr-automation pull`, then repeat the two `cp` commands and `chmod`.

### 2. Add it to the SDAF configuration menu (optional)

Edit `sap-automation/deploy/ansible/configuration_menu.sh` and add one line to each of the two arrays, at the
**same position** in both. The examples below add it after HCMT.

In the `options` array:

```bash
        "HCMT"
        "Capacity Reservations (ODCR)"          # add this line

        # Special menu entries
        "BOM Download"
```

In the `all_playbooks` array:

```bash
        ${cmd_dir}/playbook_04_00_02_db_hcmt.yaml
        ${cmd_dir}/playbook_11_00_00_capacity_reservations.yaml   # add this line
        ${cmd_dir}/playbook_bom_downloader.yaml
```

> ⚠️ The position in `options` must match the position in `all_playbooks`, otherwise the menu runs the wrong
> playbook. Re-apply this change after every SDAF upgrade.

### 3. Verify

```bash
cd ~/Azure_SAP_Automated_Deployment/WORKSPACES/SYSTEM/<SID-RG>/
~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/configuration_menu.sh
```

"Capacity Reservations (ODCR)" appears in the menu (option 15 in the SDAF v3.18 menu). Choose **Quit** - selecting
the ODCR option runs `create`.

---

## Usage

Run the tool from the SID's workspace folder on the deployer. For long runs, use `tmux` or `screen` so a dropped
SSH session doesn't stop the run.

**Recommended workflow**: `plan` → review → `create` → `info`.

| Action | What it does | Changes Azure? |
|---|---|---|
| `plan` | Dry run: shows the CRGs and reservations it would create or grow, and the VMs it would associate | No |
| `create` | Creates/grows CRGs and reservations and associates VMs; skips VMs that are already associated | **Yes** (reservations are billed) |
| `info` | Shows each reservation's Capacity / Associated / Allocated counts and each VM's CRG | No |

### Option A - SDAF configuration menu

```bash
cd ~/Azure_SAP_Automated_Deployment/WORKSPACES/SYSTEM/<SID-RG>/
~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/configuration_menu.sh
# select "Capacity Reservations (ODCR)"
```

The menu runs `create`. The menu passes its own command-line arguments to `ansible-playbook`, so the action and
any [optional setting](#optional-settings) can be added there, e.g.:

```bash
~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/configuration_menu.sh --extra-vars="odcr_action=plan"
```

Only select the ODCR option when passing ODCR variables this way.

### Option B - Run the playbook manually

```bash
cd ~/Azure_SAP_Automated_Deployment/WORKSPACES/SYSTEM/<SID-RG>/
export ANSIBLE_INVENTORY=<SID>_hosts.yaml

ansible-playbook ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/playbook_11_00_00_capacity_reservations.yaml \
  --extra-vars="_workspace_directory=$(pwd)" \
  --extra-vars="odcr_action=plan"
```

Change the last line to `odcr_action=create` or `odcr_action=info`. Without `odcr_action` the playbook runs
`create`.

Example for SID `PGY`:

```bash
cd ~/Azure_SAP_Automated_Deployment/WORKSPACES/SYSTEM/PRD-SCUS-TFO01-PGY/
export ANSIBLE_INVENTORY=PGY_hosts.yaml

ansible-playbook ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/playbook_11_00_00_capacity_reservations.yaml \
  --extra-vars="_workspace_directory=$(pwd)" \
  --extra-vars="odcr_action=plan"
```

Both settings are required when running manually (the SDAF menu sets them for you):

| Setting | Why |
|---|---|
| `ANSIBLE_INVENTORY=<SID>_hosts.yaml` | The playbook reads the SID's resource group and subscription from this file |
| `_workspace_directory=$(pwd)` | Where the inventory is, and where the completion marker (`.progress/odcr-management-done`) is written |

The playbook only runs Azure CLI commands from the deployer. It doesn't connect to the SAP VMs, so no SSH key,
`--inventory-file` or `sap-parameters.yaml` is needed.

### Option C - Run the script directly

The script can run without Ansible, for any SID resource group:

```bash
~/Azure_SAP_Automated_Deployment/sap-automation/deploy/scripts/odcr_management.sh \
  plan PRD-SCUS-TFO01-PGY <subscription-id>
```

Arguments: `<plan|create|info> <SID resource group> <subscription ID> [expected Azure region]`.
The fourth argument is optional for compatibility with older commands. If supplied, it must match the
VM-discovered region (case-insensitive); it cannot override that region. Optional settings are passed as
environment variables (see below). Exit code `0` = success, `1` = stopped or failed.

---

## Optional settings

| Ansible variable | Script environment variable | Default | Purpose |
|---|---|---|---|
| `odcr_action` | (first argument) | `create` | `plan`, `create` or `info` |
| `odcr_tier` | `ODCR_TIER` | From the resource group name | `prod` or `nonprod`; needed if the environment code isn't `PRD`/`NRD` |
| `odcr_central_resource_group` | `ODCR_CENTRAL_RG` | `<ENV>-<REGION>-CR` | Use a different central resource group |
| `odcr_central_crg_name` | `ODCR_CENTRAL_CRG` | `<REGION>-CR` | Use a different central CRG name |
| `odcr_crg_zones` | `ODCR_CRG_ZONES` | `1 2 3` | Zones for newly created CRGs (can't be changed after creation) |
| `odcr_share_subscriptions` | `ODCR_SHARE_SUBSCRIPTIONS` | none | Subscription IDs to share the central CRG with |

Examples (add to the `ansible-playbook` command):

```bash
# Share the central App server CRG between prod and non-prod (pass both IDs on every run)
--extra-vars='{"odcr_share_subscriptions": ["<prod-subscription-id>", "<nonprod-subscription-id>"]}'

# Force the tier
--extra-vars="odcr_tier=nonprod"

# Use a different central resource group / CRG
--extra-vars="odcr_central_resource_group=PRD-SCUS-CR-ALT odcr_central_crg_name=SCUS-CR-ALT"
```

Script equivalent:

```bash
ODCR_SHARE_SUBSCRIPTIONS="<prod-subscription-id>,<nonprod-subscription-id>" \
  ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/scripts/odcr_management.sh \
  create PRD-SCUS-TFO01-PGY <prod-subscription-id>
```

---

## Reading the output

The header shows what the tool resolved:

```
SID Resource Group: PRD-SCUS-TFO01-PGY
Environment/Region: PRD / SCUS
Tier:               prod
SID CRG (SCS/DB):   PRD-SCUS-TFO01-PGY/SCUS-PGY-CR
Central App CRG:    PRD-SCUS-CR/SCUS-CR
Share central with: <none>
```

Each group of VMs shows the decision, e.g.:

```
[PRD-SCUS-CR/SCUS-CR] Standard_E8ds_v5 zone 1 - 2 VM(s): ...
  Reservation SCUS-AZ01-Apps: capacity 4, associated 4, available 0
  Not enough available capacity - increasing SCUS-AZ01-Apps to 6
```

In `plan` mode every change is shown as a `[plan] az ...` line and nothing is executed.

**Summary sections**

| Section | Meaning |
|---|---|
| Associated / Would associate | VMs associated in this run (or that would be, in `plan`) |
| Skipped - already associated with a CRG | No action needed |
| Skipped - web dispatcher (not reserved) | Web Dispatchers are not reserved |
| Not supported - SKU has no ODCR support | The VM size can't be reserved in this region |
| Skipped - other | Regional VMs, or VM names without a recognised role |
| Failed | Errors - the playbook fails if this isn't 0 |
| Central CRG sharing | Sharing result (`not requested`, `already shared`, `added ...`) |

**`info` columns**

| Column | Meaning |
|---|---|
| Capacity | Reserved VM instances |
| Associated | VMs associated with the reservation (running or deallocated) |
| Allocated | VMs currently running on the reservation |

---

## Common scenarios

### New SID

Deploy the SID with SDAF, then run `plan` and `create`. The SID CRG and its reservations are created, the central
App server reservations are created or grown, and all VMs are associated.

### App servers added to an existing SID

Deploy the new app servers with SDAF, then run `plan` and `create`. Existing VMs are skipped; the central
reservation for each size/zone is grown only by the shortfall, and the new VMs are associated.

### Re-run / verification

Running `create` again is safe: if everything is already associated it reports
`Skipped - already associated with a CRG` for every VM and changes nothing. Use `info` at any time to check
the reservations.

---

## Cleanup: remove and redeploy

> ⚠️ These commands change production capacity guarantees. Review them, run them in a maintenance window and
> run `info` before and after.

Capacity reservations are billed whether VMs use them or not, so remove reservations that are no longer needed.

Set these variables first (example values shown):

```bash
SUB=<subscription-id>
SID_RG=PRD-SCUS-TFO01-PGY        # SID resource group
SID_CRG=SCUS-PGY-CR              # SID CRG (<REGION>-<SID>-CR)
CENTRAL_RG=PRD-SCUS-CR           # central resource group (<ENV>-<REGION>-CR)
CENTRAL_CRG=SCUS-CR              # central App server CRG (<REGION>-CR)
```

### A. Remove and redeploy a SID

Use this before decommissioning or rebuilding a SID. The central pool stays in place for the other SIDs.

**1. Remove the SID CRG** (the SID's SCS/DB VMs keep running). A SID resource group can't be deleted by SDAF while
it still contains the CRG.

```bash
# Set the SID reservations to 0 so the VMs can be disassociated while running
for r in $(az capacity reservation list -g $SID_RG -c $SID_CRG --subscription $SUB --query "[].name" -o tsv); do
  az capacity reservation update -g $SID_RG -c $SID_CRG -n "$r" --capacity 0 --subscription $SUB -o none && echo "$r -> 0"
done

# Disassociate the VMs that use the SID CRG (SCS / DB)
az vm list -g $SID_RG --subscription $SUB \
  --query "[?capacityReservation.capacityReservationGroup.id != null].[name, capacityReservation.capacityReservationGroup.id]" -o tsv |
while read -r vm crg; do
  if [[ "${crg,,}" == *"/capacityreservationgroups/${SID_CRG,,}" ]]; then
    az vm update -g $SID_RG -n "$vm" --capacity-reservation-group None --subscription $SUB -o none && echo "Disassociated $vm"
  fi
done

# Delete the SID reservations and the SID CRG
for r in $(az capacity reservation list -g $SID_RG -c $SID_CRG --subscription $SUB --query "[].name" -o tsv); do
  az capacity reservation delete -g $SID_RG -c $SID_CRG -n "$r" --subscription $SUB --yes && echo "Deleted $r"
done
az capacity reservation group delete -g $SID_RG -n $SID_CRG --subscription $SUB --yes && echo "Deleted $SID_CRG"
```

**2. Remove the SID** with your normal process (SDAF remover, or delete the resource group and clean up the
Terraform state). Deleting the app server VMs automatically releases their association with the central CRG.

**3. Trim the central reservations** so the capacity the SID used is no longer billed. This sets each central
reservation to the number of VMs still associated with it (VMs of other SIDs, running or deallocated). Review
first, then apply:

```bash
APPLY=false    # set to true to apply the changes
for r in $(az capacity reservation list -g $CENTRAL_RG -c $CENTRAL_CRG --subscription $SUB --query "[].name" -o tsv); do
  read -r cap assoc < <(az capacity reservation show -g $CENTRAL_RG -c $CENTRAL_CRG -n "$r" --subscription $SUB -o json \
      | jq -r '"\(.sku.capacity) \((.virtualMachinesAssociated // []) | length)"')
  if (( assoc < cap )); then
    echo "$r: capacity $cap -> $assoc"
    $APPLY && az capacity reservation update -g $CENTRAL_RG -c $CENTRAL_CRG -n "$r" --capacity "$assoc" --subscription $SUB -o none
  else
    echo "$r: capacity $cap (no change)"
  fi
done
```

Only run step 3 after the SID's VMs are deleted. It removes any extra capacity in the central pool, including
capacity kept on purpose as headroom.

**4. Redeploy the SID** with SDAF, then run the tool again (`plan`, `create`, `info`).

### B. Remove all ODCR resources for a tier and region

Use this to retire ODCR completely, e.g. for `PRD-SCUS-CR`. All VMs stay running but lose their capacity guarantee.
Run section A step 1 for every SID first, then:

```bash
# Set the central reservations to 0 so the app servers can be disassociated while running
for r in $(az capacity reservation list -g $CENTRAL_RG -c $CENTRAL_CRG --subscription $SUB --query "[].name" -o tsv); do
  az capacity reservation update -g $CENTRAL_RG -c $CENTRAL_CRG -n "$r" --capacity 0 --subscription $SUB -o none && echo "$r -> 0"
done

# Disassociate every VM that still uses the central CRG (all SIDs and subscriptions)
for r in $(az capacity reservation list -g $CENTRAL_RG -c $CENTRAL_CRG --subscription $SUB --query "[].name" -o tsv); do
  for id in $(az capacity reservation show -g $CENTRAL_RG -c $CENTRAL_CRG -n "$r" --subscription $SUB \
        --query "virtualMachinesAssociated[].id" -o tsv); do
    az vm update --ids "$id" --capacity-reservation-group None -o none && echo "Disassociated ${id##*/}"
  done
done

# Delete the central reservations and CRG (the central resource group itself is kept)
for r in $(az capacity reservation list -g $CENTRAL_RG -c $CENTRAL_CRG --subscription $SUB --query "[].name" -o tsv); do
  az capacity reservation delete -g $CENTRAL_RG -c $CENTRAL_CRG -n "$r" --subscription $SUB --yes && echo "Deleted $r"
done
az capacity reservation group delete -g $CENTRAL_RG -n $CENTRAL_CRG --subscription $SUB --yes && echo "Deleted $CENTRAL_CRG"
```

The disassociation step uses `--ids`, so it also covers VMs in the other subscription if the CRG is shared; the
identity needs rights to update those VMs.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `central App server resource group '<name>' does not exist` | Create the central resource group (`<ENV>-<REGION>-CR`), or point to another one with `odcr_central_resource_group`. Nothing was changed. |
| `environment code '<X>' does not map to a tier` | The SID resource group doesn't start with `PRD` or `NRD`. Set `odcr_tier=prod` or `odcr_tier=nonprod`. |
| `no VMs found` / `missing or invalid VM location` | Check the inventory scope and the Azure VM data. A region cannot be safely inferred from an empty or invalid VM list. |
| `multiple VM locations` | All VMs in the targeted resource group must be in one region. The tool does not partition a resource group across regions. |
| `location ... does not match VM location` | Correct a stale optional script location argument, or select a target CRG in the VM region. No Azure changes were made by this run. |
| `unable to list capacity reservation groups` / `unable to check central resource group` | Check Azure access and connectivity. A failed lookup stops the run; it is not treated as a missing CRG. |
| `Not supported - SKU has no ODCR support` | The VM size can't be reserved in this region. Those VMs are not protected. |
| `regional VM - association requires deallocation` | The VM isn't zonal. Associating it needs a deallocation in a maintenance window - not done by the tool. |
| `role not recognised from VM name` | The VM name doesn't follow the SDAF `<sid>app/scs/d/web` pattern. |
| `CRG ... does not include zone N` | The CRG was created without that zone. CRG zones can't be changed; create a new CRG or adjust the VM zone. |
| Reservation create/update fails | Usually vCPU quota or regional capacity for that VM size and zone. Request quota and re-run - finished work is kept. |
| `unable to retrieve VM sizes supporting capacity reservations` | Check the Azure CLI sign-in and `Microsoft.Compute/skus/read` on the subscription. |
| Playbook can't find the inventory / resource group is empty | Run from the SID workspace folder, with `export ANSIBLE_INVENTORY=<SID>_hosts.yaml` and `_workspace_directory=$(pwd)`. |
| `Associated` shows 0 for every reservation in `info` | You're on an old version - update the files from this repository. |
| Run interrupted (Ctrl+C or SSH disconnect) | Run `plan` to see the current state, then `create` again. Use `tmux` for long runs. |

---

## Limitations and notes

- **Cost**: reservations are billed at the VM rate whether or not VMs use them.
- **CRG sharing is an Azure preview feature**. The tool only adds subscriptions to the sharing list; moving VMs onto
  the other subscription's pool (e.g. during a failover) is a manual or runbook action and isn't automated.
- **CRG zones are fixed at creation**: new CRGs are created with zones `1 2 3` by default so failover zones are
  covered.
- **Menu integration** must be re-applied after SDAF upgrades.
- **One SID resource group, one VM region per run**: the central pool is shared, so run the tool for each SID
  after it's deployed or changed. Out-of-band VM additions are discovered on the next run; this is not a
  continuous reconciliation service.

---

## Files

| File | Install location | Purpose |
|---|---|---|
| `playbook_11_00_00_capacity_reservations.yaml` | `sap-automation/deploy/ansible/` | Ansible wrapper: reads the SID inventory, runs the script, reports results |
| `odcr_management.sh` | `sap-automation/deploy/scripts/` | Logic: CRGs, reservations, VM association, sharing |

---

## Regression tests

On Linux (including WSL), with Bash 4+, `jq`, Python 3 and Ansible installed:

```bash
python3 -B -m unittest discover -s tests -v
```

The tests run the script and playbook against a mock Azure CLI. They do not access Azure or change cloud resources.

## References

- [Azure capacity reservation overview](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-overview)
- [Associate a VM with a capacity reservation group](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-associate-vm)
- [Remove a VM association from a capacity reservation group](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-remove-vm)
- [Share a capacity reservation group](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-group-share)
- [SAP Deployment Automation Framework](https://learn.microsoft.com/azure/sap/automation/deployment-framework)

## License

MIT - see [LICENSE](LICENSE).

---

**Disclaimer**: This is not an official Microsoft product. Use at your own risk.
