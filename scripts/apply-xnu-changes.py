#!/usr/bin/env python3
"""Apply this repository's XNU changes to a prepared source tree.

Why not a hand-written patch file: a unified diff has to agree with its own hunk counts and with the
exact context of the file it lands on.  When it does not, one tool says "corrupt patch" while another
silently skips it -- and a build that skips it still succeeds, which is how 43600590879 produced a
126 MB kernel with `strings | grep 'DroidVM owner-cache'` = 0.

The edits anchor on text that occurs once and assert their own result: the anchor must be found and
the marker must be present afterwards.  The script is idempotent; the workflow runs it, lets
`git diff` write the patch (so hunk counts are git's problem), and reverts the tree so the build
applies that patch with all the others.

  0001  osfmk/vm/vm_map_store_rb.c
        A hole list that has drifted from the RB tree is a lost hint, not a reason to panic a guest
        that is otherwise fully up.  Replaces the panic statement with `return;`.  (Verified on the
        phone as a 4-byte edit: read_bytes 205 MB -> 778 MB, four vCPUs busy, exec launchd reached.)

  0002  bsd/kern/kern_exec.c -- native_owner_cache_prepare()
        Reports the owner-cache refusal through a panic when booted with owner-cache-diag=1.
        43606725375 showed it never fired, i.e. that chain reports success.

  0003  bsd/kern/kern_exec.c -- after native_cryptex_os_view()
        Reports the cryptex OS view stages, then asks the VFS whether the dyld shared cache exists at
        each of the three paths dyld might use.  43610287238 showed stage=5 status=0 -- the view is
        complete and a WebKit extension is visible through it -- yet dyld still reports "no dyld
        cache".  43613383283 answered the next question (all three paths resolve), and a panic here
        happens before dyld runs -- so it is a panic only at owner-cache-diag=2, while the initproc
        report (0004) prints the same variables at 1.

  0004  bsd/kern/kern_exit.c
        dyld's account of a failure is an exit reason string ("... no dyld cache") and the one thing
        it needs from the kernel first is the shared region.  At the initproc panic, report dyld's
        string with __shared_region_map_and_slide's result (0005) and the owner chain's stages: that
        is the difference between "the kernel refused the cache" and "the cache was mapped and dyld
        failed later".

  0005  bsd/vm/vm_unix.c
        The stock "the shared cache must be on the process root's volume or in the preboot cryptex"
        test has no case for a cache on a volume the owner chain mounted itself (the SystemOS volume,
        which it union-mounts over the OS volume at "/"), and a refusal there costs dyld the whole
        cache.  Accept the volume the owner chain recorded, and record the syscall's result for 0004.

usage: apply-xnu-changes.py <xnu source tree>
"""
import re
import sys
from pathlib import Path

FAILED = []


def read(tree, relative):
    path = tree / relative
    if not path.is_file():
        sys.exit(f'{relative}: not in {tree} -- is this the prepared xnu-work tree?')
    return path, path.read_text()


def check(name, condition, detail):
    if condition:
        print(f'  ok    {name}')
    else:
        print(f'  FAIL  {name}: {detail}')
        FAILED.append(name)


def edit_001(text):
    """Return the text with the hole-list panic replaced by a plain return."""
    anchor = 'panic("Illegal action: h1:'
    first = text.find(anchor)
    if first < 0:
        return text, False
    end = text.find(');', first)
    if end < 0:
        raise SystemExit('0001: found the panic anchor but not its closing ");"')
    # Keep the leading indentation of the statement so the surrounding braces stay readable.
    line_start = text.rfind('\n', 0, first) + 1
    indent = text[line_start:first]
    replacement = (indent + '/* DroidVM: the hole list is a hint over the RB tree, not the authority.  A missed\n'
                   + indent + ' * hole leaves first_free on an address that may be taken, and the next search\n'
                   + indent + ' * re-checks the tree and corrects it; nothing is mapped or unmapped here, so\n'
                   + indent + ' * skipping the update cannot lose an entry.  On this board the drift happens on\n'
                   + indent + ' * the first exec, with the guest otherwise fully up, so the panic cost a boot. */\n'
                   + indent + 'return;')
    # end points at the ");" that closes the panic call, so the replacement has to take both
    return text[:line_start] + replacement + text[end + 2:], True


def edit_002(text):
    """Return the text with the owner-cache diagnostic inserted after the authorization line."""
    pattern = re.compile(r'^([ \t]*)native_owner_cache_authorized = cache_authorized;[ \t]*\n', re.M)
    match = pattern.search(text)
    if not match:
        return text, False
    ind = match.group(1)          # the indentation of the statement in the function
    o = ind + '\t'                # one level deeper, inside the new if
    block = (
        f'{ind}/*\n'
        f'{ind} * DroidVM: the owner chain returns an errno and the caller goes on to exec init against\n'
        f'{ind} * whatever root it has, so the guest only fails later with dyld\'s "no dyld cache".\n'
        f'{ind} * Report the refusal here instead -- behind owner-cache-diag=1, so an ordinary build\n'
        f'{ind} * of this is inert.  stage: 0 root not authorized, 1 volume never published,\n'
        f'{ind} * 3 mount() failed (mount_status holds its errno), 4 mounted but not authorized.\n'
        f'{ind} */\n'
        f'{ind}if (error) {{\n'
        f'{o}extern volatile int native_owner_root_apple_autherr;\n'
        f'{o}extern volatile int native_owner_root_decision;\n'
        f'{o}extern int PE_parse_boot_argn(const char *, void *, int);\n'
        f'{o}int diag = 0;\n'
        f'\n'
        f'{o}if (PE_parse_boot_argn("owner-cache-diag", &diag, sizeof(diag)) && diag) {{\n'
        f'{o}\tpanic("DroidVM owner-cache: stage=%d status=%d mount=%d root_auth=%d "\n'
        f'{o}\t    "autherr=%d decision=%d dev=%d/%d path=%s",\n'
        f'{o}\t    native_owner_cache_stage, error, native_owner_cache_mount_status,\n'
        f'{o}\t    root_authorized, native_owner_root_apple_autherr, native_owner_root_decision,\n'
        f'{o}\t    (int)root_device, (int)selected, native_owner_cache_device_path);\n'
        f'{o}}}\n'
        f'{ind}}}\n'
    )
    return text[:match.end()] + block + text[match.end():], True


def edit_003(text):
    """Return the text with a diagnostic panic after the cryptex OS view is built."""
    anchor = 'native_cryptex_os_view(p, scratch);'
    first = text.find(anchor)
    if first < 0:
        return text, False
    line_start = text.rfind('\n', 0, first) + 1
    ind = text[line_start:first]
    o = ind + '\t'
    block = (
        f'{ind}{anchor}\n'
        f'{ind}/*\n'
        f'{ind} * DroidVM: the owner chain reported success and this view completes, yet dyld still says\n'
        f'{ind} * "no dyld cache".  Report every stage here, then ask the VFS whether the cache exists\n'
        f'{ind} * at each of the three places dyld might look.\n'
        f'{ind} */\n'
        f'{ind}{{\n'
        f'{o}extern volatile int native_cryptex_os_stage, native_cryptex_os_status;\n'
        f'{o}extern volatile int native_gpu_addon_stage, native_gpu_addon_status;\n'
        f'{o}extern volatile int native_gpu_addon2_stage, native_gpu_addon2_status;\n'
        f'{o}extern volatile int native_gpu_addon3_stage, native_gpu_addon3_status;\n'
        f'{o}extern volatile int native_owner_cache_stage, native_owner_cache_status;\n'
        f'{o}extern volatile int native_owner_cache_mount_status;\n'
        f'{o}extern volatile int native_owner_data_stage, native_owner_data_status;\n'
        f'{o}extern volatile int native_staged_apps_stage, native_staged_view_stage, native_staged_view_status;\n'
        f'{o}extern volatile int native_owner_boot_manifest_status;\n'
        f'{o}extern char native_owner_cache_device_path[64];\n'
        f'{o}extern int PE_parse_boot_argn(const char *, void *, int);\n'
        f'{o}/* Ask the kernel which of the three places dyld might look actually has the cache.\n'
        f'{o} * The real typedefs, not void*: a conflicting redeclaration of vnode_lookup would not\n'
        f'{o} * compile, and kern_exec.c already has these headers (it mounts and looks up paths). */\n'
        f'{o}vnode_t vp = NULLVP;\n'
        f'{o}vfs_context_t vctx = vfs_context_current();\n'
        f'{o}int c1, c2, c3;\n'
        f'{o}c1 = vnode_lookup("/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64", 0, &vp, vctx);\n'
        f'{o}if (!c1) {{ vnode_put(vp); vp = NULLVP; }}\n'
        f'{o}c2 = vnode_lookup("/System/Cryptexes/OS/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64", 0, &vp, vctx);\n'
        f'{o}if (!c2) {{ vnode_put(vp); vp = NULLVP; }}\n'
        f'{o}c3 = vnode_lookup("/private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64", 0, &vp, vctx);\n'
        f'{o}if (!c3) {{ vnode_put(vp); vp = NULLVP; }}\n'
        f'{o}/* owner-cache-diag=2 asks for this panic.  At 1 the same variables are printed by the\n'
        f'{o} * initproc report (kern_exit.c), because a panic here happens before dyld runs and the\n'
        f'{o} * question is what dyld does with these volumes, not whether they resolve. */\n'
        f'{o}int diag = 0;\n'
        f'{o}if (PE_parse_boot_argn("owner-cache-diag", &diag, sizeof(diag)) && diag >= 2) {{\n'
        f'{o}\tpanic("DroidVM cryptex: cache=%d/%d/%d stage=%d status=%d addon=%d/%d addon2=%d/%d addon3=%d/%d "\n'
        f'{o}\t    "owner=%d/%d/%d data=%d/%d staged=%d/%d/%d manifest=%d path=%s",\n'
        f'{o}\t    c1, c2, c3,\n'
        f'{o}\t    native_cryptex_os_stage, native_cryptex_os_status,\n'
        f'{o}\t    native_gpu_addon_stage, native_gpu_addon_status,\n'
        f'{o}\t    native_gpu_addon2_stage, native_gpu_addon2_status,\n'
        f'{o}\t    native_gpu_addon3_stage, native_gpu_addon3_status,\n'
        f'{o}\t    native_owner_cache_stage, native_owner_cache_status, native_owner_cache_mount_status,\n'
        f'{o}\t    native_owner_data_stage, native_owner_data_status,\n'
        f'{o}\t    native_staged_apps_stage, native_staged_view_stage, native_staged_view_status,\n'
        f'{o}\t    native_owner_boot_manifest_status, native_owner_cache_device_path);\n'
        f'{o}}}\n'
        f'{ind}}}\n'
    )
    return text[:line_start] + block + text[first + len(anchor):], True


def edit_004(text):
    """Return kern_exit.c with the initproc panic reporting the dyld-cache side of the failure."""
    anchor = '\tproc_crash_coredump(p);\n'
    first = text.find(anchor)
    if first < 0:
        return text, False
    block = (
        '\t/*\n'
        '\t * DroidVM: dyld\'s account of a failure is an exit reason string, and the one thing it\n'
        '\t * needs from the kernel before any lookup is the shared region: __shared_region_map_and_slide\n'
        '\t * (vm_unix.c records its result in droidvm_sr_*).  If that call was refused, dyld runs with\n'
        '\t * no cache at all and every later message blames "no dyld cache".  Report it together with\n'
        '\t * the owner chain\'s stages, which the cryptex diagnostic used to report from before dyld.\n'
        '\t */\n'
        '\tif (p == initproc) {\n'
        '\t\textern int droidvm_sr_calls, droidvm_sr_files, droidvm_sr_mappings, droidvm_sr_result;\n'
        '\t\textern int droidvm_sr_refusals, droidvm_sr_file_dev, droidvm_sr_root_dev;\n'
        '\t\textern volatile int native_cryptex_os_stage, native_cryptex_os_status;\n'
        '\t\textern volatile int native_gpu_addon3_stage, native_gpu_addon3_status;\n'
        '\t\textern volatile int native_owner_cache_stage, native_owner_cache_status;\n'
        '\t\textern volatile int native_owner_cache_native_union;\n'
        '\t\tpanic("DroidVM initproc: %s: %.160s | sr=%d(f%d/m%d)=%d refuse=%d fdev=%d rdev=%d "\n'
        '\t\t    "cryptex=%d/%d addon3=%d/%d owner=%d/%d union=%d",\n'
        '\t\t    prefix_str, exit_reason_desc ? exit_reason_desc : "none",\n'
        '\t\t    droidvm_sr_calls, droidvm_sr_files, droidvm_sr_mappings, droidvm_sr_result,\n'
        '\t\t    droidvm_sr_refusals, droidvm_sr_file_dev, droidvm_sr_root_dev,\n'
        '\t\t    native_cryptex_os_stage, native_cryptex_os_status,\n'
        '\t\t    native_gpu_addon3_stage, native_gpu_addon3_status,\n'
        '\t\t    native_owner_cache_stage, native_owner_cache_status,\n'
        '\t\t    native_owner_cache_native_union);\n'
        '\t}\n'
    )
    return text[:first] + block + text[first:], True


def edit_005(text):
    """Return vm_unix.c accepting the owner cache volume, and recording why the shared region failed.

    Three anchors, all plain statements: the _shared_region_map_and_slide() definition (for the
    globals and helpers), the "must be on the process root's volume" test (for the acceptance and the
    refusal counter) and the call in the syscall wrapper (for the result dyld sees)."""
    fn_anchor = 'static int\n_shared_region_map_and_slide(\n'
    cond_anchor = '\t\tif (srfmp->vp->v_mount != rdir_vp->v_mount) {\n'
    call_anchor = ('\tkr = _shared_region_map_and_slide(p, files_count, shared_files, mappings_count, mappings);\n'
                   'done:\n')
    for anchor in (fn_anchor, cond_anchor, call_anchor):
        if text.find(anchor) < 0:
            return text, False

    helper = (
        '/*\n'
        ' * DroidVM: the dyld cache lives on the SystemOS volume, which the owner chain mounted itself\n'
        ' * and put over the OS volume at "/".  The stock test below accepts only "the same mount as the\n'
        ' * process root" or the preboot cryptex, and a refusal there fails the whole\n'
        ' * __shared_region_map_and_slide -- dyld then runs with no cache at all ("no dyld cache" for\n'
        ' * every lookup), which is what this guest reported while every cache path still resolved.\n'
        ' * Accept the volume the owner chain mounted as its cache; nothing else matches it, so an\n'
        ' * ordinary kernel keeps the stock rule.  The four droidvm_sr_* values are what the initproc\n'
        ' * report prints, and they say whether a refusal happened at all.\n'
        ' */\n'
        'int droidvm_sr_calls = 0;\n'
        'int droidvm_sr_files = 0;\n'
        'int droidvm_sr_mappings = 0;\n'
        'int droidvm_sr_result = 0;\n'
        'int droidvm_sr_refusals = 0;\n'
        'int droidvm_sr_file_dev = -1;\n'
        'int droidvm_sr_root_dev = -1;\n'
        '\n'
        'static dev_t\n'
        'droidvm_vnode_device(vnode_t vp)\n'
        '{\n'
        '\tmount_t m;\n'
        '\tvnode_t devvp;\n'
        '\tdev_t dev;\n'
        '\n'
        '\tif (vp == NULLVP || (m = vnode_mount(vp)) == NULL ||\n'
        '\t    (devvp = vfs_devvp(m)) == NULLVP) {\n'
        '\t\treturn NODEV;\n'
        '\t}\n'
        '\tdev = vnode_specrdev(devvp);\n'
        '\tvnode_put(devvp);\n'
        '\treturn dev;\n'
        '}\n'
        '\n'
        'static boolean_t\n'
        'droidvm_owner_cache_volume(vnode_t vp)\n'
        '{\n'
        '\textern volatile dev_t native_owner_cache_mounted_device;\n'
        '\tdev_t dev = droidvm_vnode_device(vp);\n'
        '\n'
        '\treturn dev != NODEV && dev == native_owner_cache_mounted_device;\n'
        '}\n'
        '\n'
    )
    text = text.replace(fn_anchor, helper + fn_anchor, 1)
    text = text.replace(
        cond_anchor,
        '\t\tdroidvm_sr_file_dev = (int)droidvm_vnode_device(srfmp->vp);\n'
        '\t\tdroidvm_sr_root_dev = (int)droidvm_vnode_device(rdir_vp);\n'
        '\t\tif (!droidvm_owner_cache_volume(srfmp->vp) &&\n'
        '\t\t    srfmp->vp->v_mount != rdir_vp->v_mount) {\n'
        '\t\t\tdroidvm_sr_refusals++;\n', 1)
    text = text.replace(
        call_anchor,
        '\tkr = _shared_region_map_and_slide(p, files_count, shared_files, mappings_count, mappings);\n'
        '\t/* DroidVM: what dyld is told.  A failure here is the difference between a guest with the\n'
        '\t * dyld cache mapped and one whose every lookup answers "no dyld cache". */\n'
        '\tdroidvm_sr_calls++;\n'
        '\tdroidvm_sr_files = (int)files_count;\n'
        '\tdroidvm_sr_mappings = (int)mappings_count;\n'
        '\tdroidvm_sr_result = (int)kr;\n'
        'done:\n', 1)
    return text, True


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    tree = Path(sys.argv[1])
    if not (tree / 'bsd').is_dir() or not (tree / 'osfmk').is_dir():
        sys.exit(f'{tree} does not look like an XNU source tree')

    print('0001 osfmk/vm/vm_map_store_rb.c')
    path, text = read(tree, 'osfmk/vm/vm_map_store_rb.c')
    if 'Illegal action: h1:' not in text:
        check('panic replaced', 'DroidVM: the hole list is a hint' in text,
              'the anchor is gone and so is the comment: already applied or a different source?')
    else:
        new, applied = edit_001(text)
        check('anchor found', applied, 'no "Illegal action: h1:" in the file')
        check('comment present', 'DroidVM: the hole list is a hint' in new, 'replacement did not land')
        check('panic gone', 'Illegal action: h1:' not in new, 'the panic is still there')
        path.write_text(new)

    print('0002 bsd/kern/kern_exec.c (owner cache)')
    path, text = read(tree, 'bsd/kern/kern_exec.c')
    if 'DroidVM owner-cache' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_002(text)
        check('anchor found', applied, 'no "native_owner_cache_authorized = cache_authorized;"')
        check('panic present', 'DroidVM owner-cache' in new, 'replacement did not land')
        if applied:
            path.write_text(new)

    print('0003 bsd/kern/kern_exec.c (cryptex view and cache paths)')
    path, text = read(tree, 'bsd/kern/kern_exec.c')
    if 'DroidVM cryptex:' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_003(text)
        check('cryptex anchor found', applied, 'no "native_cryptex_os_view(p, scratch);"')
        check('cryptex panic present', 'DroidVM cryptex:' in new, 'replacement did not land')
        if applied:
            path.write_text(new)

    print('0004 bsd/kern/kern_exit.c (initproc report)')
    path, text = read(tree, 'bsd/kern/kern_exit.c')
    if 'DroidVM initproc:' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_004(text)
        check('initproc anchor found', applied, 'no "\tproc_crash_coredump(p);"')
        check('initproc report present', 'DroidVM initproc:' in new, 'replacement did not land')
        if applied:
            path.write_text(new)

    print('0005 bsd/vm/vm_unix.c (owner cache volume and the syscall result)')
    path, text = read(tree, 'bsd/vm/vm_unix.c')
    if 'droidvm_owner_cache_volume' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_005(text)
        check('vm_unix anchors found', applied,
              'one of the three vm_unix.c anchors is missing: the patched source changed')
        check('helper present', 'droidvm_owner_cache_volume' in new, 'helper did not land')
        check('condition changed', '!droidvm_owner_cache_volume(srfmp->vp) &&' in new, 'condition not rewritten')
        check('recorder present', 'droidvm_sr_result = (int)kr;' in new, 'recorder did not land')
        if applied:
            path.write_text(new)

    if FAILED:
        sys.exit('apply-xnu-changes: ' + ', '.join(FAILED))
    print('all changes are in the source the compiler will read')


if __name__ == '__main__':
    main()
