import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("ODCR_SCRIPT_UNDER_TEST", ROOT / "odcr_management.sh"))
PLAYBOOK = Path(os.environ.get(
    "ODCR_PLAYBOOK_UNDER_TEST", ROOT / "playbook_11_00_00_capacity_reservations.yaml"
))
SUB = "11111111-1111-1111-1111-111111111111"
RG = "PRD-SCUS-TFO01-PGY"
CENTRAL_RG = "PRD-SCUS-CR"

MOCK_AZ = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with open(os.environ["AZ_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\n")
config = json.loads(Path(os.environ["AZ_FIXTURE"]).read_text())

def value(flag):
    return args[args.index(flag) + 1]

def emit(data):
    print(json.dumps(data))

if value("--subscription") != config["subscription"]:
    sys.exit("Unexpected subscription")
if config.get("fail_command") and args[:len(config["fail_command"])] == config["fail_command"]:
    sys.exit("Azure lookup denied")
if args[:2] == ["group", "show"]:
    if "--query" in args:
        sys.exit("Do not derive VM location from resource-group metadata")
    emit({"location": "eastus"})
elif args[:2] == ["group", "exists"]:
    print("true" if config.get("central_exists", True) else "false")
elif args[:2] == ["vm", "list"]:
    if value("--resource-group") != config["resource_group"]:
        sys.exit("Unexpected resource group")
    query = value("--query")
    if "location:location" not in query or "id:id" not in query:
        sys.exit("VM discovery must request ID and location")
    if "raw_vms" in config:
        print(config["raw_vms"])
    else:
        emit(config["vms"])
elif args[:4] == ["capacity", "reservation", "group", "list"]:
    emit(config.get("groups", {}).get(value("--resource-group"), []))
elif args[:4] == ["capacity", "reservation", "group", "show"]:
    if "--query" in args and "sharingProfile" in value("--query"):
        print("")
    else:
        sys.exit("Unexpected group show")
elif args[:3] == ["capacity", "reservation", "list"]:
    emit(config.get("reservations", []))
elif args[:3] == ["capacity", "reservation", "show"]:
    emit(config["reservations"][0])
elif args[:2] == ["vm", "list-skus"]:
    if value("--location") != config.get("expected_location", "southcentralus"):
        sys.exit("Incorrect SKU query location")
    print("Standard_D4ds_v5\nStandard_E20ds_v4")
elif any(word in args for word in ("create", "update")):
    if "--location" in args and value("--location") != config.get("expected_location", "southcentralus"):
        sys.exit("Incorrect resource creation location")
else:
    sys.exit("Unexpected az call: " + repr(args))
'''


def vm(name="pgyapp01", location="southcentralus", crg=None):
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Compute/virtualMachines/{name}",
        "name": name, "location": location, "size": "Standard_D4ds_v5",
        "zone": "1", "crg": crg,
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        az = self.bin / "az"
        az.write_text(MOCK_AZ)
        az.chmod(0o755)
        self.fixture_path = self.work / "fixture.json"
        self.calls_path = self.work / "calls.jsonl"
        self.fixture = {"subscription": SUB, "resource_group": RG, "vms": [vm()]}
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("ODCR_", "ANSIBLE_"))
        }
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            AZ_FIXTURE=str(self.fixture_path), AZ_CALLS=str(self.calls_path),
            ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(self.work / "ansible-temp"),
        )

    def run_script(self, action="create", *extra):
        self.fixture_path.write_text(json.dumps(self.fixture))
        return subprocess.run(
            ["bash", str(SCRIPT), action, RG, SUB, *extra],
            env=self.env, text=True, capture_output=True, timeout=30,
        )

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def mutations(self):
        return [call for call in self.calls() if "create" in call or "update" in call]

    def assert_stopped(self, message):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertEqual(self.mutations(), [])

    def test_region_comes_from_vms_not_resource_group(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Location:           southcentralus", result.stdout)
        self.assertTrue(self.mutations())
        self.assertEqual(sum(call[:2] == ["vm", "list"] for call in self.calls()), 1)

    def test_plan_discovers_additional_vm_without_mutations(self):
        self.fixture["vms"].append(vm("pgyapp02"))
        result = self.run_script("plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would associate: 2", result.stdout)
        self.assertIn("--capacity 2", result.stdout)
        self.assertEqual(self.mutations(), [])

    def test_empty_resource_group_stops(self):
        self.fixture["vms"] = []
        self.assert_stopped("no VMs found")

    def test_missing_vm_location_stops(self):
        self.fixture["vms"][0].pop("location")
        self.assert_stopped("VM location")

    def test_invalid_vm_responses_stop(self):
        for response in ("not-json", "null", "{}", "", "[null]"):
            with self.subTest(response=response):
                self.calls_path.unlink(missing_ok=True)
                self.fixture["raw_vms"] = response
                self.assert_stopped("ERROR:")

    def test_invalid_vm_locations_stop(self):
        for location in (None, "", "South Central US", 123):
            with self.subTest(location=location):
                self.calls_path.unlink(missing_ok=True)
                self.fixture["vms"] = [vm(location=location)]
                self.assert_stopped("VM location")

    def test_mixed_vm_regions_stop(self):
        self.fixture["vms"].append(vm("pgyapp02", "eastus"))
        self.assert_stopped("multiple VM locations")

    def test_legacy_location_argument_must_match(self):
        result = self.run_script("create", "eastus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertEqual(self.mutations(), [])

    def test_legacy_matching_location_argument_works(self):
        result = self.run_script("plan", "SouthCentralUS")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_crg_in_other_region_stops_before_any_creation(self):
        self.fixture["groups"] = {
            RG: [{"name": "SCUS-PGY-CR", "location": "eastus", "zones": ["1"]}]
        }
        self.assert_stopped("does not match")

    def test_central_crg_in_other_region_stops(self):
        self.fixture["groups"] = {
            CENTRAL_RG: [{"name": "SCUS-CR", "location": "eastus", "zones": ["1"]}]
        }
        self.assert_stopped("does not match")

    def test_crg_without_location_stops(self):
        self.fixture["groups"] = {RG: [{"name": "SCUS-PGY-CR", "zones": ["1"]}]}
        self.assert_stopped("invalid location")

    def test_invalid_crg_response_stops(self):
        self.fixture["groups"] = {RG: None}
        self.assert_stopped("invalid capacity reservation group data")

    def test_existing_matching_crgs_are_reused(self):
        self.fixture["groups"] = {
            CENTRAL_RG: [{"name": "SCUS-CR", "location": "southcentralus", "zones": ["1"]}]
        }
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(call[:4] == ["capacity", "reservation", "group", "create"]
                             for call in self.calls()))

    def test_lookup_errors_stop_before_mutations(self):
        for command in (["vm", "list"], ["group", "exists"],
                        ["capacity", "reservation", "group", "list"]):
            with self.subTest(command=command):
                self.calls_path.unlink(missing_ok=True)
                self.fixture["fail_command"] = command
                self.assert_stopped("ERROR:")

    def test_missing_central_rg_still_stops(self):
        self.fixture["central_exists"] = False
        self.assert_stopped("does not exist")

    def test_info_is_read_only(self):
        result = self.run_script("info")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pgyapp01", result.stdout)
        self.assertEqual(self.mutations(), [])

    def test_info_handles_missing_central_rg(self):
        self.fixture["central_exists"] = False
        result = self.run_script("info")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Not found", result.stdout)
        self.assertEqual(self.mutations(), [])

    def test_existing_associations_and_role_skips_are_preserved(self):
        self.fixture["vms"] = [
            vm(crg="/existing/crg"), vm("pgyweb01"), vm("unrecognised"),
            vm("pgyapp02"),
        ]
        self.fixture["vms"][-1]["zone"] = None
        result = self.run_script("plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would associate: 0", result.stdout)
        self.assertIn("Skipped - already associated with a CRG: 1", result.stdout)
        self.assertIn("Skipped - web dispatcher (not reserved): 1", result.stdout)
        self.assertIn("Skipped - other: 2", result.stdout)
        self.assertEqual(self.mutations(), [])

    def test_scs_and_db_still_use_sid_crg(self):
        self.fixture["vms"] = [vm("pgyscs01"), vm("pgydhdb01")]
        self.fixture["vms"][1]["size"] = "Standard_E20ds_v4"
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = [call for call in self.calls() if call[:2] == ["vm", "update"]]
        self.assertEqual(len(updates), 2)
        for call in updates:
            target = call[call.index("--capacity-reservation-group") + 1]
            self.assertIn(f"/resourceGroups/{RG}/", target)
            self.assertTrue(target.endswith("/SCUS-PGY-CR"))
        self.assertFalse(any(call[:2] == ["group", "exists"] for call in self.calls()))

    def test_reservation_reuse_and_growth_are_preserved(self):
        for capacity, associated, expected_capacity in ((3, 1, None), (1, 1, 2), (1, 2, 3)):
            with self.subTest(capacity=capacity, associated=associated):
                self.calls_path.unlink(missing_ok=True)
                self.fixture["groups"] = {
                    CENTRAL_RG: [{"name": "SCUS-CR", "location": "southcentralus", "zones": ["1"]}]
                }
                self.fixture["reservations"] = [{
                    "name": "SCUS-AZ01-Apps", "sku": {"name": "Standard_D4ds_v5", "capacity": capacity},
                    "zones": ["1"], "virtualMachinesAssociated": [
                        {"id": f"/vm/{index}"} for index in range(associated)
                    ],
                }]
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                changes = [call for call in self.calls()
                           if call[:3] == ["capacity", "reservation", "update"]]
                if expected_capacity is None:
                    self.assertEqual(changes, [])
                else:
                    self.assertEqual(len(changes), 1)
                    self.assertEqual(changes[0][changes[0].index("--capacity") + 1], str(expected_capacity))
                self.assertEqual(sum(call[:2] == ["vm", "update"] for call in self.calls()), 1)

    def run_playbook(self, inventory_text):
        ansible = shutil.which("ansible-playbook")
        self.assertIsNotNone(ansible, "ansible-playbook is required for integration tests")
        deploy = self.work / "deploy"
        (deploy / "ansible").mkdir(parents=True)
        (deploy / "scripts").mkdir()
        playbook = deploy / "ansible" / PLAYBOOK.name
        shutil.copyfile(PLAYBOOK, playbook)
        shutil.copyfile(SCRIPT, deploy / "scripts" / SCRIPT.name)
        inventory = self.work / "PGY_hosts.yaml"
        inventory.write_text(inventory_text)
        self.env["ANSIBLE_INVENTORY"] = inventory.name
        self.fixture_path.write_text(json.dumps(self.fixture))
        return subprocess.run(
            [ansible, str(playbook), "-e", json.dumps({
                "_workspace_directory": str(self.work), "odcr_action": "plan",
            })], cwd=self.work, env=self.env, capture_output=True, text=True, timeout=90,
        )

    def test_playbook_inventory_selects_scope_not_vm_membership(self):
        self.fixture["vms"].append(vm("pgyapp02"))
        result = self.run_playbook(
            f"all:\n  hosts:\n    pgyapp01:\n      resource_group_name: {RG}\n"
            f"      subscription_id: {SUB}\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would associate: 2", result.stdout)
        self.assertIn("southcentralus", result.stdout)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.work / ".progress").exists())

    def test_missing_inventory_subscription_stops_before_azure(self):
        result = self.run_playbook(
            f"all:\n  hosts:\n    pgyapp01:\n      resource_group_name: {RG}\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
