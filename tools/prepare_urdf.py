#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn the vendor URDF export into this deployment's whole-robot URDF.

The vendor model (``JAKA-Lumi-sensors-v3``) is a SolidWorks export that carries
sensor frames but speaks its own joint names and uses ``package://`` mesh URIs.
Robonix needs three things it does not provide:

1. **Arm joints named ``joint_1..joint_6``** — the reused arm primitive
   hardcodes ``JOINT_NAMES = joint_1..joint_6`` and ``robot_state_publisher``
   matches by name, so ``l_a1..l_a6`` would be silently ignored.
2. **Relative mesh paths** — a deployment repository is not an ament package,
   so ``package://<pkg>/meshes/...`` never resolves.
3. **Passive joints declared fixed** — wheels and casters have no encoder
   feedback, so as ``continuous`` joints they would simply be missing from the
   TF tree. As ``fixed`` they are published once on ``/tf_static``.

``l_1..l_4`` (lift, waist, head yaw, head pitch) keep their vendor names: the
body primitive publishes exactly those.

Usage:
    python3 tools/prepare_urdf.py \
        --source "$LUMI_MODEL/urdf/JAKA-Lumi-sensors-v3.urdf" \
        --vendor-meshes "$LUMI_MODEL/meshes"

The script is idempotent and verifies its own output; a failed check exits 1.
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: Vendor joint -> deployment joint, so the arm primitive's names match.
ARM_RENAMES = {f"l_a{index}": f"joint_{index}" for index in range(1, 7)}

#: Joints with no feedback. Declared fixed so they land in /tf_static instead
#: of being dropped from the tree.
PASSIVE_JOINTS = (
    "w_r",
    "w_l",
    "pw1_1",
    "pw1_2",
    "pw2_1",
    "pw2_2",
    "pw3_1",
    "pw3_2",
    "pw4_1",
    "pw4_2",
)

#: Body joints published by lumi_body.
BODY_JOINTS = ("l_1", "l_2", "l_3", "l_4")

#: The CAD export has the drive-wheel axle along X (wheels at x = +-0.179) and
#: the front sensors at negative Y, so the model's forward axis is -Y. ROS
#: (REP-103) wants X forward, Y left, Z up. Wrapping the chassis in a new root
#: ``base_link`` rotated by this yaw maps model -Y onto robot +X:
#:   R(+90 deg) * (0, -1, 0) = (1, 0, 0)
#: and keeps the wheel axle lateral: (0, +-0.179, -0.207) -> (-0.05, +-0.179, ...).
#: Set to 0 to keep the vendor frame untouched (no wrapper at all).
BASE_YAW_DEG = 90.0

ROBOT_NAME = "jaka_lumi"
ROOT_LINK = "base_link"


def _local_mesh(filename: str) -> str:
    """``package://<pkg>/meshes/x.STL`` -> ``meshes/x.STL``."""
    if filename.startswith("package://"):
        _, _, remainder = filename[len("package://") :].partition("/")
        return remainder
    return filename


def transform(source: Path) -> ET.Element:
    tree = ET.parse(source)
    root = tree.getroot()
    if root.tag != "robot":
        raise SystemExit(f"{source}: root element is <{root.tag}>, expected <robot>")
    root.set("name", ROBOT_NAME)

    renamed = 0
    fixed = 0
    for joint in root.iter("joint"):
        name = joint.get("name", "")
        if name in ARM_RENAMES:
            joint.set("name", ARM_RENAMES[name])
            renamed += 1
        elif name in PASSIVE_JOINTS:
            joint.set("type", "fixed")
            for child in list(joint):
                if child.tag in (
                    "axis",
                    "limit",
                    "dynamics",
                    "calibration",
                    "safety_controller",
                ):
                    joint.remove(child)
            fixed += 1

    for tag in ("visual", "collision"):
        for element in root.iter(tag):
            mesh = element.find("geometry/mesh")
            if mesh is not None and mesh.get("filename"):
                mesh.set("filename", _local_mesh(mesh.get("filename", "")))

    if renamed != len(ARM_RENAMES):
        raise SystemExit(
            f"{source}: renamed {renamed} arm joints, expected {len(ARM_RENAMES)}"
        )
    if fixed != len(PASSIVE_JOINTS):
        raise SystemExit(
            f"{source}: converted {fixed} passive joints, expected {len(PASSIVE_JOINTS)}"
        )

    _wrap_base_link(root)
    return root


def _wrap_base_link(root: ET.Element) -> None:
    """Insert a REP-103 ``base_link`` above the CAD chassis link.

    The CAD root (``base_link``) keeps its geometry but is renamed
    ``base_link_chassis`` and hangs off a new rotation-only joint, so every
    provider can publish ``base_link`` with X forward, Y left.
    """
    if BASE_YAW_DEG == 0.0:
        return

    chassis = None
    for link in root.iter("link"):
        if link.get("name") == "base_link":
            chassis = link
            break
    if chassis is None:
        raise SystemExit("no base_link root found to wrap")
    chassis.set("name", "base_link_chassis")

    # Anything parented to the old root now hangs off the chassis link.
    for joint in root.iter("joint"):
        parent = joint.find("parent")
        if parent is not None and parent.get("link") == "base_link":
            parent.set("link", "base_link_chassis")

    base_link = ET.SubElement(root, "link", {"name": "base_link"})
    base_link.tail = "\n"

    joint = ET.SubElement(root, "joint", {"name": "base_link_to_chassis", "type": "fixed"})
    joint.tail = "\n"
    ET.SubElement(joint, "origin", {"xyz": "0 0 0", "rpy": f"0 0 {math.radians(BASE_YAW_DEG):.6f}"})
    ET.SubElement(joint, "parent", {"link": "base_link"})
    ET.SubElement(joint, "child", {"link": "base_link_chassis"})


def verify(urdf: Path, meshes_dir: Path) -> list[str]:
    """Return a list of human-readable problems; empty means healthy."""
    problems: list[str] = []
    root = ET.parse(urdf).getroot()

    joints = {joint.get("name"): joint for joint in root.iter("joint")}
    links = [link.get("name") for link in root.iter("link")]
    children = {
        child.get("link")
        for joint in joints.values()
        if (child := joint.find("child")) is not None
    }
    roots = [name for name in links if name not in children]
    if roots != [ROOT_LINK]:
        problems.append(f"root link is {roots}, expected ['{ROOT_LINK}']")

    movable = {name for name, joint in joints.items() if joint.get("type") != "fixed"}
    expected = set(BODY_JOINTS) | set(ARM_RENAMES.values())
    if movable != expected:
        problems.append(
            f"movable joints {sorted(movable)} != expected {sorted(expected)}"
        )

    for name in PASSIVE_JOINTS:
        joint = joints.get(name)
        if joint is None:
            problems.append(f"passive joint {name} disappeared")
        elif joint.get("type") != "fixed":
            problems.append(f"passive joint {name} is still {joint.get('type')}")

    if BASE_YAW_DEG != 0.0:
        if "base_link_chassis" not in links:
            problems.append("wrapper link base_link_chassis is missing")
        wrapper = joints.get("base_link_to_chassis")
        if wrapper is None:
            problems.append("wrapper joint base_link_to_chassis is missing")
        else:
            origin = wrapper.find("origin")
            expected_rpy = f"{math.radians(BASE_YAW_DEG):.6f}"
            actual = (origin.get("rpy", "") if origin is not None else "").split()
            if len(actual) != 3 or abs(float(actual[2]) - float(expected_rpy)) > 1e-6:
                problems.append(
                    f"wrapper yaw is {actual}, expected {math.radians(BASE_YAW_DEG):.6f} rad"
                )

    for element in root.iter("mesh"):
        filename = element.get("filename", "")
        if filename.startswith("package://"):
            problems.append(f"unresolved package URI left: {filename}")
            continue
        if not (meshes_dir / Path(filename).name).is_file():
            problems.append(f"missing mesh file: {filename}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the deployment URDF from the vendor export."
    )
    parser.add_argument(
        "--source", required=True, type=Path, help="vendor JAKA-Lumi-sensors-v3.urdf"
    )
    parser.add_argument(
        "--vendor-meshes", required=True, type=Path, help="vendor meshes/ directory"
    )
    parser.add_argument("--out", type=Path, default=Path("urdf/jaka_lumi.urdf"))
    parser.add_argument("--out-meshes", type=Path, default=Path("urdf/meshes"))
    args = parser.parse_args()

    if not args.source.is_file():
        raise SystemExit(f"source URDF not found: {args.source}")
    if not args.vendor_meshes.is_dir():
        raise SystemExit(f"vendor meshes not found: {args.vendor_meshes}")

    root = transform(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out_meshes.mkdir(parents=True, exist_ok=True)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(args.out, encoding="utf-8", xml_declaration=True)

    copied = 0
    for mesh in root.iter("mesh"):
        name = Path(mesh.get("filename", "")).name
        target = args.out_meshes / name
        source = args.vendor_meshes / name
        if not source.is_file():
            raise SystemExit(f"vendor mesh missing: {source}")
        if not target.is_file():
            copied += 1
        shutil.copyfile(source, target)

    problems = verify(args.out, args.out_meshes)
    print(f"[prepare_urdf] wrote {args.out} ({args.out.stat().st_size} bytes)")
    print(f"[prepare_urdf] {copied} new meshes copied into {args.out_meshes}")
    print(
        "[prepare_urdf] renamed "
        f"{len(ARM_RENAMES)} arm joints, fixed {len(PASSIVE_JOINTS)} passive joints, "
        f"movable = {len(BODY_JOINTS) + len(ARM_RENAMES)}"
    )
    if problems:
        for problem in problems:
            print(f"[prepare_urdf] PROBLEM: {problem}", file=sys.stderr)
        return 1
    print("[prepare_urdf] verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
