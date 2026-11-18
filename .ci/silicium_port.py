#!/usr/bin/env python3
"""Mechanical parts of a Mu-Silicium Snapdragon port, from the Project Silicium
"Adding Devices" / "Adding SoCs" guides (which that project marks as outdated).

Inputs : device.dts (dtc output of the phone's device tree), uefiplat.cfg (from UEFIReader on xbl)
Outputs: report.md                    GIC / timer / CPU values, per the guide's conversion rules
         DeviceMemoryMapLib.c         body regenerated inside an existing device's file
         DeviceConfigurationMapLib.c  same
         edits to a copied SoC .dsc.inc and a copied device .conf

It never invents a file layout: memory/config maps are written INTO template files
copied from a similar existing device, so includes and boilerplate stay correct.
"""
import argparse, re, sys

# ---------- device tree ----------
def node_body(text, header_re):
    m = re.search(header_re + r"\s*\{", text)
    if not m:
        return None
    i, depth = m.end(), 1
    while depth and i < len(text):
        depth += (text[i] == "{") - (text[i] == "}")
        i += 1
    return text[m.end():i - 1]

def prop(body, name):
    if body is None:
        return None
    m = re.search(r"(?m)^\s*" + re.escape(name) + r"\s*=\s*<([^>]*)>", body)
    return [int(x, 16) if x.startswith("0x") else int(x) for x in m.group(1).split()] if m else None

def pairs(cells):
    """reg cells -> [(addr,size)]; handles 1 or 2 address/size cells."""
    if not cells:
        return []
    step = 4 if len(cells) % 4 == 0 and len(cells) >= 4 and cells[0] == 0 and cells[2] == 0 else 2
    out = []
    for i in range(0, len(cells) - step + 1, step):
        if step == 4:
            out.append(((cells[i] << 32) | cells[i + 1], (cells[i + 2] << 32) | cells[i + 3]))
        else:
            out.append((cells[i], cells[i + 1]))
    return out

def ppi(t, n):
    return n + (0x10 if t == 1 else 0x20)  # PPI base 16, SPI base 32

def dts_values(dts):
    v = {}
    gic = None
    for m in re.finditer(r"interrupt-controller@[0-9a-f]+", dts):
        b = node_body(dts, re.escape(m.group(0)))
        if b and "arm,gic-v3" in b and "msi" not in m.group(0):
            gic = b
            break
    if gic:
        regs = pairs(prop(gic, "reg"))
        v["gic_dist"] = regs[0][0]
        v["gic_redist"] = regs[1][0] if len(regs) > 1 else None
        v["gic_msi"] = bool(re.search(r"msi-controller@", gic))
    t = prop(node_body(dts, r"\n\s*timer"), "interrupts")
    if t and len(t) >= 12:
        v["timer"] = [ppi(t[i], t[i + 1]) for i in (0, 3, 6, 9)]  # Sec, NonSec, Virt, Hyp
    p = prop(node_body(dts, r"\n\s*cpu-pmu"), "interrupts")
    if p and len(p) >= 2:
        v["pmu"] = ppi(p[0], p[1])
    v["cores"] = len(set(re.findall(r"\n\s*cpu@(\w+)\s*\{", dts)))
    v["clusters"] = len(set(re.findall(r"\bcluster(\d+)\s*\{", dts)))
    return v

# ---------- uefiplat.cfg ----------
def split_csv(line):
    return [x.strip() for x in re.findall(r'"[^"]*"|[^,]+', line)]

def parse_cfg(path):
    mem, conf, sec = [], [], None
    for raw in open(path, errors="replace"):
        line = raw.split("//")[0].rstrip()
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            sec = s.strip("[]").lower()
        elif sec == "memorymap":
            mem.append(split_csv(s))
        elif sec == "configparameters" and "=" in s:
            k, val = [x.strip() for x in s.split("=", 1)]
            if k != "ConfigParameterCount" and re.fullmatch(r"0x[0-9a-fA-F]+|\d+", val):
                conf.append((k, val))
    return mem, conf

def patch_array(src, array_name, terminator_re, new_lines):
    pat = re.compile(r"(" + array_name + r"\[\]\s*=\s*\{)(.*?)(\n\s*(?://[^\n]*\n\s*)?" + terminator_re + ")", re.S)
    if not pat.search(src):
        sys.exit(f"template has no {array_name}[] array with a terminator")
    return pat.sub(lambda m: m.group(1) + "\n" + "\n".join(new_lines) + m.group(3), src, count=1)

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--dts", required=True)
    a.add_argument("--cfg", required=True)
    a.add_argument("--memmap-template", required=True, help="existing DeviceMemoryMapLib.c to copy from")
    a.add_argument("--confmap-template", required=True, help="existing DeviceConfigurationMapLib.c")
    a.add_argument("--out-memmap", required=True)
    a.add_argument("--out-confmap", required=True)
    a.add_argument("--soc-dsc-inc", help="copied SoC .dsc.inc to patch in place")
    a.add_argument("--device-conf", help="copied Resources/Configs/<device>.conf to patch in place")
    a.add_argument("--report", default="report.md")
    o = a.parse_args()

    v = dts_values(open(o.dts, errors="replace").read())
    mem, conf = parse_cfg(o.cfg)

    # memory map: guide maps "Addr, Len, "Name", rest..." -> {"Name", Addr, Len, rest...},
    lines, fd = [], None
    for t in mem:
        if len(t) < 8:
            continue
        addr, size, name, rest = t[0], t[1], t[2].strip('"'), t[3:8]
        lines.append(f'  {{"{name}", {addr}, {size}, {", ".join(rest)}}},')
        if name.lower() in ("uefi fd", "uefi_fd"):
            fd = (addr, size)
    src = open(o.memmap_template).read()
    open(o.out_memmap, "w").write(patch_array(src, "gDeviceMemoryDescriptorEx", r'\{\s*"Terminator"', lines))

    clines = [f'  {{"{k}", {val}}},' for k, val in conf]
    src = open(o.confmap_template).read()
    open(o.out_confmap, "w").write(patch_array(src, "gDeviceConfigurationDescriptorEx", r'\{\s*"Terminator"', clines))

    # SoC .dsc.inc PCDs
    if o.soc_dsc_inc:
        s = open(o.soc_dsc_inc).read()
        def setpcd(name, val):
            nonlocal s
            s, n = re.subn(r"(" + name + r"\s*\|\s*)(\S+)", lambda m: m.group(1) + str(val), s)
            return n
        done = {}
        if "timer" in v:
            for nm, val in zip(("PcdArmArchTimerSecIntrNum", "PcdArmArchTimerIntrNum",
                                "PcdArmArchTimerVirtIntrNum", "PcdArmArchTimerHypIntrNum"), v["timer"]):
                done[nm] = setpcd(nm, val)
        if "gic_dist" in v:
            done["PcdGicDistributorBase"] = setpcd("PcdGicDistributorBase", hex(v["gic_dist"]))
        if v.get("gic_redist"):
            done["PcdGicRedistributorsBase"] = setpcd("PcdGicRedistributorsBase", hex(v["gic_redist"]))
            done["PcdGicInterruptInterfaceBase"] = setpcd("PcdGicInterruptInterfaceBase", hex(v["gic_redist"]))
        if v["cores"]:
            done["PcdCoreCount"] = setpcd("PcdCoreCount", v["cores"])
        if v["clusters"]:
            done["PcdClusterCount"] = setpcd("PcdClusterCount", v["clusters"])
        open(o.soc_dsc_inc, "w").write(s)
        v["patched_pcds"] = done

    if o.device_conf and fd:
        s = open(o.device_conf).read()
        size = int(fd[1], 16) if fd[1].startswith("0x") else int(fd[1])
        for key, val in (("TARGET_FD_BASE", fd[0]), ("TARGET_FD_SIZE", fd[1]), ("TARGET_FD_BLOCKS", hex(size // 0x1000))):
            s = re.sub(r'(?m)^(' + key + r'=).*$', lambda m: f'{m.group(1)}"{val}"', s)
        open(o.device_conf, "w").write(s)

    with open(o.report, "w") as r:
        r.write("# Port report\n\n")
        for k, val in v.items():
            r.write(f"- {k}: {val}\n")
        r.write(f"\n- memory regions written: {len(lines)}\n- config parameters written: {len(clines)}\n")
        r.write(f"- UEFI FD region: {fd}\n")
        r.write("\nSoC values to double-check by hand (guide says the timer PCDs can also be read from TimerDxe.efi): "
                "PcdHasLevel3Cache, PcdIsPowerOkImplemented, USE_PHYSICAL_TIMER, SmBios CPU speeds and cache sizes.\n")
        if v.get("gic_msi"):
            r.write("\nGIC has an msi-controller: ACPI APIC needs the Generic MSI Frame block (guide, step 1.1).\n")
    print(open(o.report).read())

if __name__ == "__main__":
    main()
