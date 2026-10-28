#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Static checks for this deployment repository.

Nothing here talks to the robot. The goal is to catch the mistakes that a YAML
reviewer misses and that only surface at `rbnx boot` time:

  * a `path:` package or a `manifest:` file that does not exist,
  * a `params_file` that is not there,
  * a ``${VAR}`` that boot would expand to an empty string,
  * a soma component pointing at a URDF link/joint that was renamed away,
  * a soma `provider_id` that is not a manifest instance,
  * a sana capability that the referenced package does not declare,
  * an unresolvable ``__ROBONIX_*__`` token left in a Nav2 params file,
  * a URDF whose movable joints do not match what the providers publish.

Usage:
    python3 tools/check_deployment.py            # local manifest if present
    python3 tools/check_deployment.py -m robonix_manifest.yaml
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

#: Joints lumi_body publishes (lumi_body/driver.py JOINT_NAMES).
BODY_JOINTS = {"l_1", "l_2", "l_3", "l_4"}
#: Joints the reused arm primitive publishes (jaka_arm/driver.py JOINT_NAMES).
ARM_JOINTS = {f"joint_{index}" for index in range(1, 7)}

#: Tokens the navigation wrapper can resolve, mapped to the provider role that
#: must be bound for them to resolve.
TOKEN_ROLES = {
    "__ROBONIX_MAP_TOPIC__": "map",
    "__ROBONIX_ODOM_TOPIC__": "odom",
    "__ROBONIX_SCAN_TOPIC__": "scan",
    "__ROBONIX_SCAN_CLOUD_TOPIC__": "scan_cloud",
    "__ROBONIX_FOOTPRINT__": "footprint",
    "__ROBONIX_BT_XML__": "bt_xml",
}
#: Roles the wrapper fills from Soma / the deployment itself, not from Atlas.
LOCAL_ROLES = {"footprint", "bt_xml"}

problems: list[str] = []
notes: list[str] = []
skipped: list[str] = []

#: Set by --partial: a soma export whose provider is absent from this profile is
#: reported instead of failed. Used for the control-only manifest, which drops
#: the camera/mapping/navigation instances on purpose.
partial_profile = False


def fail(message: str) -> None:
    problems.append(message)


def ok(message: str) -> None:
    notes.append(message)


def skip(message: str) -> None:
    skipped.append(message)


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top level is not a mapping")
    return data


# ── checks ───────────────────────────────────────────────────────────────
def check_manifest(manifest_path: Path) -> tuple[dict, dict[str, dict]]:
    manifest = load_yaml(manifest_path)
    if manifest.get("manifestVersion") != 1:
        fail(f"{manifest_path.name}: manifestVersion is {manifest.get('manifestVersion')!r}")
    for field in ("name",):
        if not manifest.get(field):
            fail(f"{manifest_path.name}: missing `{field}`")
    catalog = manifest.get("catalog") or {}
    for field in ("name", "version", "description", "license", "tags", "maintainers"):
        if not catalog.get(field):
            fail(f"{manifest_path.name}: catalog.{field} is empty")

    entries: list[tuple[str, dict]] = []
    for section in ("primitive", "service", "skill"):
        for entry in manifest.get(section) or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                fail(f"{manifest_path.name}: {section} entry without a name")
                continue
            entries.append((section, entry))

    instances: dict[str, dict] = {}
    for section, entry in entries:
        name = entry["name"]
        if name in instances:
            fail(f"duplicate instance name `{name}`")
        instances[name] = {**entry, "_section": section}

        has_url, has_path = bool(entry.get("url")), bool(entry.get("path"))
        if has_url == has_path:
            fail(f"{name}: exactly one of `url` / `path` is required")
            continue
        package_dir: Path | None = None
        if has_path:
            package_dir = (manifest_path.parent / entry["path"]).resolve()
            if not package_dir.is_dir():
                fail(f"{name}: path {entry['path']} does not exist ({package_dir})")
                package_dir = None
        manifest_file = entry.get("manifest")
        if manifest_file and package_dir is not None:
            if not (package_dir / manifest_file).is_file():
                fail(f"{name}: manifest `{manifest_file}` not found in {package_dir}")
        instances[name]["_dir"] = package_dir

        # Service config files must exist next to the manifest.
        config = entry.get("config") or {}
        for key in ("params_file", "bt_xml_file"):
            value = config.get(key)
            if isinstance(value, str) and value and "${" not in value:
                target = (manifest_path.parent / value).resolve()
                if not target.is_file():
                    fail(f"{name}: config.{key} -> {value} does not exist")
        if config.get("bt_xml_file") is None:
            params = config.get("params_file")
            if isinstance(params, str):
                text = _read(manifest_path.parent / params)
                if text and "__ROBONIX_BT_XML__" in text:
                    fail(f"{name}: params_file uses the BT token but no bt_xml_file")
    ok(f"manifest: {len(instances)} instances, {len(entries)} entries")
    return manifest, instances


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def check_required_env(manifest_path: Path, manifest: dict, instances: dict) -> None:
    """Every ${VAR} must be provided by `env:`, .vlmkey or the shell."""
    text = _read(manifest_path)
    declared = set((manifest.get("env") or {}).keys())
    secret_file = _read(REPO / ".vlmkey")
    for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text):
        if name in declared or name == "PWD":
            continue
        if re.search(rf"\b{name}=", secret_file):
            continue
        if name.startswith("VLM_") or name.startswith("LUMI_"):
            fail(
                f"${{{name}}} is not in manifest env and not exported by .vlmkey "
                f"(copy .vlmkey.example and fill it in)"
            )
    ok("env: manifest variables resolve (or come from .vlmkey)")


def check_soma(manifest_path: Path, manifest: dict, instances: dict) -> None:
    soma_path = manifest_path.parent / "soma.yaml"
    if not soma_path.is_file():
        fail("soma.yaml is missing")
        return
    soma = load_yaml(soma_path)

    urdf_path = (soma_path.parent / (soma.get("urdf") or {}).get("path", "")).resolve()
    if not urdf_path.is_file():
        fail(f"soma.yaml: urdf.path -> {urdf_path} does not exist")
        return
    root = ET.parse(urdf_path).getroot()
    links = {link.get("name") for link in root.iter("link")}
    joints = {joint.get("name"): joint for joint in root.iter("joint")}

    declared_root = (soma.get("urdf") or {}).get("root_link")
    children = {
        child.get("link")
        for joint in joints.values()
        if (child := joint.find("child")) is not None
    }
    roots = [name for name in links if name not in children]
    if roots != [declared_root]:
        fail(f"soma.yaml: urdf.root_link={declared_root!r} but the URDF roots are {roots}")

    robot = soma.get("robot") or {}
    if not robot.get("id"):
        fail("soma.yaml: robot.id is empty")

    footprint = robot.get("footprint") or {}
    points = footprint.get("points") or []
    if len(points) < 3:
        fail("soma.yaml: footprint needs at least 3 points")
    if footprint.get("base_frame") not in links:
        fail(f"soma.yaml: footprint.base_frame {footprint.get('base_frame')!r} is not a URDF link")

    moved = {j for j, joint in joints.items() if joint.get("type") != "fixed"}
    expected = BODY_JOINTS | ARM_JOINTS
    if moved != expected:
        fail(f"URDF movable joints {sorted(moved)} != providers expect {sorted(expected)}")

    def walk(component: dict, path: str) -> None:
        label = f"{path}/{component.get('id')}"
        link = component.get("urdf_link")
        if link and link not in links:
            fail(f"soma.yaml: {label} urdf_link {link!r} is not a URDF link")
        joint = component.get("urdf_joint")
        if joint and joint not in joints:
            fail(f"soma.yaml: {label} urdf_joint {joint!r} is not a URDF joint")
        for export in component.get("exports") or []:
            provider = export.get("provider_id")
            if provider not in instances:
                if partial_profile:
                    skip(f"soma {label} exports {provider!r} (not part of this profile)")
                    continue
                fail(f"soma.yaml: {label} exports provider {provider!r} that no manifest entry defines")
                continue
            for capability in export.get("capabilities") or []:
                contract = capability.get("path")
                if not contract or not contract.startswith("robonix/"):
                    fail(f"soma.yaml: {label} has a malformed capability {contract!r}")
                    continue
                _check_capability_declared(provider, contract, instances)
        for child in component.get("components") or []:
            walk(child, label)

    for component in robot.get("components") or []:
        walk(component, "robot")
    ok(f"soma: {len(links)} URDF links, {len(joints)} joints, footprint {len(points)} points")


def _check_capability_declared(provider: str, contract: str, instances: dict) -> None:
    package_dir = instances[provider].get("_dir")
    if package_dir is None:
        return  # url-based package: not resolvable offline
    manifest_path = package_dir / (instances[provider].get("manifest") or "package_manifest.yaml")
    try:
        package = load_yaml(manifest_path)
    except (OSError, SystemExit):
        fail(f"{provider}: cannot read {manifest_path.name}")
        return
    declared = {
        capability.get("name")
        for capability in (package.get("capabilities") or [])
        if isinstance(capability, dict)
    }
    if contract not in declared:
        fail(f"{provider}: soma lists {contract} but the package manifest does not declare it")


def check_nav2_tokens(manifest_path: Path, instances: dict) -> None:
    navigation = instances.get("navigation")
    if navigation is None:
        ok("nav2: no navigation instance, token check skipped")
        return
    params = (navigation.get("config") or {}).get("params_file")
    if not isinstance(params, str):
        return
    text = _read(manifest_path.parent / params)
    bound = {
        role
        for role, value in ((navigation.get("config") or {}).get("provider_ids") or {}).items()
        if value
    }
    for token, role in TOKEN_ROLES.items():
        if token not in text:
            continue
        if role in LOCAL_ROLES:
            continue
        if role not in bound:
            fail(f"nav2 params uses {token} but provider_ids.{role} is not bound")
    ok(f"nav2: tokens resolve against provider_ids {sorted(bound)}")


def check_meshes(manifest_path: Path) -> None:
    urdf_path = manifest_path.parent / "urdf" / "jaka_lumi.urdf"
    if not urdf_path.is_file():
        fail("urdf/jaka_lumi.urdf is missing (run tools/prepare_urdf.py)")
        return
    meshes = manifest_path.parent / "urdf" / "meshes"
    missing = []
    for element in ET.parse(urdf_path).getroot().iter("mesh"):
        filename = element.get("filename", "")
        if filename.startswith("package://") or not (meshes / Path(filename).name).is_file():
            missing.append(filename)
    if missing:
        fail(f"URDF references {len(missing)} missing/unresolved meshes, e.g. {missing[0]}")
    else:
        ok(f"urdf: every mesh resolves under urdf/meshes ({meshes})")


def main() -> int:
    global partial_profile
    parser = argparse.ArgumentParser(description="Static checks for this deployment.")
    parser.add_argument(
        "-m", "--manifest", type=Path,
        default=REPO / "robonix_manifest.local.yaml",
        help="manifest to check (default: the local one when present)",
    )
    parser.add_argument(
        "--partial", action="store_true",
        help="a profile that intentionally leaves some providers out "
             "(e.g. robonix_manifest.control.yaml); soma exports for the "
             "missing ones are reported instead of failed",
    )
    args = parser.parse_args()
    partial_profile = args.partial
    manifest_path = args.manifest
    if not manifest_path.is_file():
        manifest_path = REPO / "robonix_manifest.yaml"
    if not manifest_path.is_file():
        raise SystemExit(f"manifest not found: {args.manifest}")
    manifest_path = manifest_path.resolve()

    manifest, instances = check_manifest(manifest_path)
    check_required_env(manifest_path, manifest, instances)
    check_soma(manifest_path, manifest, instances)
    check_nav2_tokens(manifest_path, instances)
    check_meshes(manifest_path)

    print(f"checked {manifest_path.name}")
    for note in notes:
        print(f"  ok   {note}")
    for note in skipped:
        print(f"  skip {note}")
    for problem in problems:
        print(f"  FAIL {problem}", file=sys.stderr)
    print(f"{len(notes)} checks passed, {len(problems)} failed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
