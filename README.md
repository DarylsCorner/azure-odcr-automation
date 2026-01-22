# Azure ODCR Automation for SDAF

[![Azure](https://img.shields.io/badge/Azure-0078D4?style=flat&logo=microsoftazure&logoColor=white)](https://azure.microsoft.com)
[![SAP](https://img.shields.io/badge/SAP-0FAAFF?style=flat&logo=sap&logoColor=white)](https://www.sap.com)
[![Shell Script](https://img.shields.io/badge/Shell_Script-121011?style=flat&logo=gnu-bash&logoColor=white)](https://www.gnu.org/software/bash/)
[![Ansible](https://img.shields.io/badge/Ansible-EE0000?style=flat&logo=ansible&logoColor=white)](https://www.ansible.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](http://makeapullrequest.com)

> **Automated Azure On-Demand Capacity Reservation (ODCR) management for SAP workloads deployed via the SAP Deployment Automation Framework (SDAF)**

## Overview

This project provides automated management of Azure On-Demand Capacity Reservations (ODCR) for SAP workloads deployed via SDAF. It guarantees VM capacity availability in specific Azure availability zones for production SAP systems, DR scenarios, and high-availability deployments.

**Validation Status**: Tested with SAP S/4HANA 2023 HA deployment
- 3 capacity reservations across 2 availability zones (Sweden Central)
- 5 VMs protected: 2x SCS (D4ds_v5), 2x App (D4ds_v5), 1x HANA DB (E20ds_v4)
- Verified via Azure Portal and CLI

## Features

- **Create Mode**: Discovers VMs, creates Capacity Reservation Group, reserves capacity per VM SKU/zone, associates VMs
- **Info Mode**: Displays reservation status, lists reservations, shows VM associations

## Prerequisites

- Azure CLI installed and configured
- Azure RBAC permissions: `Microsoft.Compute/capacityReservationGroups/*`, `Microsoft.Compute/capacityReservations/*`, `Microsoft.Compute/virtualMachines/write`
- SAP system deployed via SDAF with `sap-parameters.yaml`
- Ansible 2.11+
- VMs deployed in availability zones

## Quick Start

### Via Configuration Menu (Recommended)

```bash
cd ~/Azure_SAP_Automated_Deployment/WORKSPACES/SYSTEM/<SID>/
~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/configuration_menu.sh
# Select Option 15: "Capacity Reservations (ODCR)". This assigned number can be any available number of your choosing in the playbook menu.
```

### Direct Ansible Execution

```bash
# Create reservations
ansible-playbook ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/playbook_08_00_00_capacity_reservations.yaml \
  --inventory-file=X00_hosts.yaml --private-key=sshkey --extra-vars="@sap-parameters.yaml" --extra-vars="odcr_action=create"

# View info
ansible-playbook ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/playbook_08_00_00_capacity_reservations.yaml \
  --inventory-file=X00_hosts.yaml --private-key=sshkey --extra-vars="@sap-parameters.yaml" --extra-vars="odcr_action=info"
```
## Configuration

Variables (auto-derived from `sap-parameters.yaml`):
- `odcr_action`: `create` or `info` (default: `create`)
- `resource_group_name`: SAP VM resource group
- `capacity_reservation_group_name`: CRG name (default: `<rg-name>-crg`)
- `subscription_id`: Azure subscription
- `location`: Azure region

## Integration with SDAF Configuration Menu

### Step 1: Copy Files to SDAF Directory

```bash
# Copy playbook
cp playbook_08_00_00_capacity_reservations.yaml \
  ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/

# Copy script
cp odcr_management.sh \
  ~/Azure_SAP_Automated_Deployment/sap-automation/deploy/scripts/

# Make script executable
chmod +x odcr_management.sh
```

### Step 2: Edit configuration_menu.sh

**File**: `~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/configuration_menu.sh`

**Add menu display name to the `options` array** (around line 122):

```bash
options=(
        # ... existing entries ...
        "HCMT"
        "Capacity Reservations (ODCR)"  # Add this line
        
        # Special menu entries
        "BOM Download"
```

**Add playbook path to the `all_playbooks` array** (around line 153):

```bash
all_playbooks=(
        # ... existing playbooks ...
        ${cmd_dir}/playbook_04_00_02_db_hcmt.yaml
        ${cmd_dir}/playbook_08_00_00_capacity_reservations.yaml  # Add this line
        ${cmd_dir}/playbook_bom_downloader.yaml
```

**Important**: The position in both arrays must match - if you add it as the 15th item in `options`, it must be the 15th item in `all_playbooks`.

### Step 3: Verify Integration

```bash
cd ~/Azure_SAP_Automated_Deployment/WORKSPACES/SYSTEM/<SID>/
~/Azure_SAP_Automated_Deployment/sap-automation/deploy/ansible/configuration_menu.sh
```

You should see **Option 15: Capacity Reservations (ODCR)** in the menu.

## Example Test Results

**Environment**: SAP S/4HANA 2023 HA in Sweden Central

| VM | SKU | Zone | Role |
|---|---|---|---|
| x00scs01le2e | Standard_D4ds_v5 | 1 | ASCS |
| x00scs02le2e | Standard_D4ds_v5 | 3 | ERS |
| x00app01le2e | Standard_D4ds_v5 | 1 | App |
| x00app02le2e | Standard_D4ds_v5 | 3 | App |
| x00dhdb01l0e2e | Standard_E20ds_v4 | 1 | HANA DB |

**Result**: 3 reservations created
- `Standard_D4ds_v5-z1`: Capacity 2
- `Standard_D4ds_v5-z3`: Capacity 2
- `Standard_E20ds_v4-z1`: Capacity 1

## Verification

```bash
# Check CRG status
az capacity reservation group show \
  --resource-group <RG-NAME> \
  --name <RG-NAME>-crg

# List reservations
az capacity reservation list \
  --resource-group <RG-NAME> \
  --capacity-reservation-group <RG-NAME>-crg \
  --output table

# Verify VM associations
az vm list \
  --resource-group <RG-NAME> \
  --query '[].{Name:name, CRG:capacityReservation.capacityReservationGroup.id}' \
  --output table
```

**Azure Portal**: Navigate to Resource Group → Filter by "Capacity Reservation" → View CRG → Check "Resources" tab

## Best Practices

- **Timing**: Run after Terraform deployment, before or after SAP installation
- **Production**: Recommended to use ODCR for production SAP landscapes  
- **Cost**: Reservations charged whether VMs running or not - use for critical systems only
- **Multi-zone**: Playbook auto-creates separate reservations per zone
- **Cleanup**: Remove unused reservations promptly

## Files

1. **playbook_08_00_00_capacity_reservations.yaml** (60 lines)
   - Ansible playbook wrapper
   - Calls `odcr_management.sh` with parameters from `sap-parameters.yaml`
   
2. **odcr_management.sh** (116 lines)
   - Bash script with Azure CLI commands
   - Functions: Create CRG, create reservations per SKU/zone, associate VMs, display info

## References

- [Azure Capacity Reservations Documentation](https://learn.microsoft.com/azure/virtual-machines/capacity-reservation-overview)
- [Azure SAP Deployment Automation Framework](https://github.com/Azure/sap-automation)
- [SDAF Deployment Framework](https://learn.microsoft.com/azure/sap/automation/deployment-framework)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

**Disclaimer**: This is not an official Microsoft product. Use at your own risk.

