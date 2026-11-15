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
        Record what __shared_region_map_and_slide returned (calls / files / mappings / result) for
        0004.  On the phone it read sr=2(f54/m62)=22: the syscall runs twice and fails with EINVAL,
        i.e. the kernel refuses the dyld cache.  That rules out the stock root-volume test (EPERM).

  0006  bsd/vm/vm_unix.c
        EINVAL has two dozen possible sources.  Number every `goto done` in setup(), remember its
        errno, and record vm_shared_region_map_file()'s raw kern_return_t, so a live read of
        droidvm_sr_stage / droidvm_sr_setup_error / droidvm_sr_vmkr names the exact branch.

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
        '\t\textern volatile int native_cryptex_os_stage, native_cryptex_os_status;\n'
        '\t\textern volatile int native_gpu_addon3_stage, native_gpu_addon3_status;\n'
        '\t\textern volatile int native_owner_cache_stage, native_owner_cache_status;\n'
        '\t\textern volatile int native_owner_cache_native_union;\n'
        '\t\tpanic("DroidVM initproc: %s: %.160s | sr=%d(f%d/m%d)=%d cryptex=%d/%d addon3=%d/%d "\n'
        '\t\t    "owner=%d/%d union=%d",\n'
        '\t\t    prefix_str, exit_reason_desc ? exit_reason_desc : "none",\n'
        '\t\t    droidvm_sr_calls, droidvm_sr_files, droidvm_sr_mappings, droidvm_sr_result,\n'
        '\t\t    native_cryptex_os_stage, native_cryptex_os_status,\n'
        '\t\t    native_gpu_addon3_stage, native_gpu_addon3_status,\n'
        '\t\t    native_owner_cache_stage, native_owner_cache_status,\n'
        '\t\t    native_owner_cache_native_union);\n'
        '\t}\n'
    )
    return text[:first] + block + text[first:], True


def edit_005(text):
    """Return vm_unix.c recording what __shared_region_map_and_slide decided.

    Nothing here calls into the VFS: four globals and four assignments at the syscall's return, which
    is what the initproc report needs to say whether the kernel refused the dyld cache (and with which
    errno) or mapped it and dyld failed later.  Two rounds died in the bsd build on a richer version
    of this edit (strnlen/struct vfsstatfs, then an unproven helper); a recorder that only assigns
    cannot be the reason a kernel does not compile."""
    call_anchor = ('\tkr = _shared_region_map_and_slide(p, files_count, shared_files, mappings_count, mappings);\n'
                   'done:\n')
    fn_anchor = 'static int\n_shared_region_map_and_slide(\n'
    for anchor in (call_anchor, fn_anchor):
        if text.find(anchor) < 0:
            return text, False

    globals_ = (
        '/*\n'
        ' * DroidVM: what dyld is told.  __shared_region_map_and_slide_2_np is the only way dyld gets\n'
        ' * the shared cache mapped system-wide, and a failure there leaves it with no cache at all\n'
        ' * ("no dyld cache" for every lookup, which is what this guest reported while every cache path\n'
        ' * still resolved).  The value is dumped by the initproc report in kern_exit.c.\n'
        ' */\n'
        'int droidvm_sr_calls = 0;\n'
        'int droidvm_sr_files = 0;\n'
        'int droidvm_sr_mappings = 0;\n'
        'int droidvm_sr_result = 0;\n'
        '\n'
    )
    text = text.replace(fn_anchor, globals_ + fn_anchor, 1)
    text = text.replace(
        call_anchor,
        '\tkr = _shared_region_map_and_slide(p, files_count, shared_files, mappings_count, mappings);\n'
        '\tdroidvm_sr_calls++;\n'
        '\tdroidvm_sr_files = (int)files_count;\n'
        '\tdroidvm_sr_mappings = (int)mappings_count;\n'
        '\tdroidvm_sr_result = (int)kr;\n'
        'done:\n', 1)
    return text, True


def edit_006(text):
    """Return vm_unix.c recording which shared-region check refused the dyld cache.

    0005 says only that the syscall returned EINVAL (22).  setup() has two dozen
    `goto done` sites and the caller turns any vm_shared_region_map_file() failure
    into that same EINVAL, so one number is not enough to choose a fix.  Record the
    branch index, the errno setup() returned, and the raw kern_return_t -- all plain
    assignments to file-scope ints, readable live from guest RAM after boot.
    """
    # Anchor on the whole "static int\n<name>(" pair.  Inserting just before the name would
    # put the globals between `static int` and the function name -- a syntax error that only
    # shows up 18 minutes later as a make recursion error with no diagnostic (43662467314).
    # If UniXNU's tree spells the return-type line differently, step back one whole line from
    # the name instead; either way the globals land at file scope, before the definition.
    name_only = '\nshared_region_map_and_slide_setup(\n'
    definition = '\nstatic int' + name_only
    name_at = text.find(name_only)
    fn_end = text.find('\n_shared_region_map_and_slide(', name_at)
    if name_at < 0 or fn_end < 0:
        return text, False
    exact = text.find(definition)
    if exact >= 0:
        insert_at = exact + 1
        print('    0006 globals: exact "static int" anchor')
    else:
        insert_at = text.rfind('\n', 0, name_at) + 1
        print('    0006 globals: fallback, line above the function name')

    globals_ = (
        '/*\n'
        ' * DroidVM: which check in shared_region_map_and_slide_setup() turned dyld away.\n'
        ' * sr_stage is the 1-based index of that function\'s `goto done` sites,\n'
        ' * sr_setup_error the errno it returned, sr_vmkr the raw kern_return_t from\n'
        ' * vm_shared_region_map_file() (-1 when setup failed first).  Live-readable.\n'
        ' */\n'
        'int droidvm_sr_stage = 0;\n'
        'int droidvm_sr_setup_error = 0;\n'
        'int droidvm_sr_vmkr = -1;\n'
        '\n'
    )
    text = text[:insert_at] + globals_ + text[insert_at:]

    # Prove in the CI log that the globals were not placed between the return type and the
    # function name.  run14's only symptom was a make recursion error with no diagnostic.
    for line in text[insert_at:insert_at + 360].splitlines()[:12]:
        print('    0006 | ' + line)

    # Recompute the span from the function name (the comment above does not contain that line).
    fn_start = text.find(name_only)
    fn_end = text.find('\n_shared_region_map_and_slide(', fn_start)
    span = text[fn_start:fn_end]
    counter = [0]

    def mark(m):
        counter[0] += 1
        # The CI tree carries UniXNU's own vm_unix.c patches, so the branch numbering can only
        # be interpreted against the tree actually compiled.  Print the map into the build log.
        before = [x.strip() for x in span[:m.start()].splitlines()[-5:] if x.strip()]
        print('    0006 stage %2d: %s' % (counter[0], ' | '.join(before)[-170:]))
        # Both statements stay on one line: several callers use an unbraced `if (error) goto done;`.
        return '%sdroidvm_sr_stage = %d; goto done;' % (m.group(1), counter[0])

    span, n = re.subn(r'(?m)^([ \t]*)goto done;[ \t]*$', mark, span)
    if n < 10:
        return text, False
    label = 'done:\n\tif (error != 0) {'
    if label not in span:
        return text, False
    span = span.replace(label, 'done:\n\tdroidvm_sr_setup_error = (int)error;\n\tif (error != 0) {', 1)
    text = text[:fn_start] + span + text[fn_end:]

    kr_anchor = '\tkr = vm_shared_region_map_file(shared_region, files_count, sr_file_mappings);\n'
    if kr_anchor not in text:
        return text, False
    text = text.replace(kr_anchor, kr_anchor + '\tdroidvm_sr_vmkr = (int)kr;\n', 1)
    return text, True


def edit_007(text):
    """0007  osfmk/vm/vm_shared_region.c -- name the KERN_FAILURE in vm_shared_region_map_file().

    run15 live read: sr_stage=0, sr_setup_error=0, sr_vmkr=5 (KERN_FAILURE).
    So all 23 BSD-side checks passed and the refusal is in the VM layer.
    In xnu-10063.141.1 only three sites in this path can produce KERN_FAILURE:
      1  setup(): shared_region->sr_first_mapping != -1 ("caught race in map and slide")
      2  setup(): copyin() failed with an errno other than EPERM/EACCES/EFAULT
      3  map_file(): vm_shared_region_slide() failed (slide info sanity check
         v1..v5, shared_region_pager_match(), etc.)
    Also record setup's kr, slide's kr/index and the number of slide mappings.
    All names start with droidvm_sr_ so tools/p3sr.py finds them via LC_SYMTAB.
    """
    # The definition is preceded by __attribute__((noinline)).  Anchor the whole
    # prefix: inserting between the attribute and `static kern_return_t` breaks
    # the declaration (the run14 failure mode).
    definition = ('\n__attribute__((noinline))\n'
                  'static kern_return_t\nvm_shared_region_map_file_setup(\n')
    fn = text.find(definition)
    if fn < 0:
        definition = '\nstatic kern_return_t\nvm_shared_region_map_file_setup(\n'
        fn = text.find(definition)
    if fn < 0:
        return text, False

    globals_ = '''
/* DroidVM 0007: live-readable recorders for the VM-side shared-region refusal. */
int droidvm_sr_vmstage = 0;             /* 1=race, 2=copyin, 3=slide, 0=other */
int droidvm_sr_vm_setup_kr = -1;
int droidvm_sr_vm_slide_kr = -1;
int droidvm_sr_vm_slide_index = -1;
int droidvm_sr_vm_first_mapping = -2;
int droidvm_sr_vm_slides = -1;

'''
    # Put the globals before `static kern_return_t`, never between the return
    # type and the function name (run14 wasted 18 minutes on exactly that).
    text = text[:fn + 1] + globals_ + text[fn + 1:]

    a = '\t/*\n\t * Figure out how many of the mappings have slides.\n\t */\n'
    if a not in text:
        return text, False
    reset = ('\tdroidvm_sr_vmstage = 0;\n'
             '\tdroidvm_sr_vm_setup_kr = -1;\n'
             '\tdroidvm_sr_vm_slide_kr = -1;\n'
             '\tdroidvm_sr_vm_slide_index = -1;\n'
             '\tdroidvm_sr_vm_first_mapping = -2;\n')
    text = text.replace(a, reset + a, 1)

    a = '\t/* Allocate per slide data structures */\n'
    if a not in text:
        return text, False
    text = text.replace(a, '\tdroidvm_sr_vm_slides = (int)vmsr_num_slides;\n' + a, 1)

    # 1: someone already mapped this shared region
    a = ('\tif (shared_region->sr_first_mapping != -1) {\n'
         '\t\tvm_shared_region_unlock();\n'
         '#if DEVELOPMENT || DEBUG\n')
    b = ('\tif (shared_region->sr_first_mapping != -1) {\n'
         '\t\tvm_shared_region_unlock();\n'
         '\t\tdroidvm_sr_vmstage = 1;\n'
         '\t\tdroidvm_sr_vm_first_mapping = (int)shared_region->sr_first_mapping;\n'
         '#if DEVELOPMENT || DEBUG\n')
    if a not in text:
        return text, False
    text = text.replace(a, b, 1)

    # 2: copyin() default branch
    text, n = re.subn(r'(default:[ \t]*\n[ \t]*)kr = KERN_FAILURE;',
                      r'\1droidvm_sr_vmstage = 2; kr = KERN_FAILURE;',
                      text, count=1)
    if n != 1:
        return text, False

    # setup() failed: remember its raw kr before bailing out
    a = ('\tif (kr != KERN_SUCCESS) {\n'
         '\t\tvm_shared_region_lock();\n'
         '\t\tgoto done;\n'
         '\t}\n'
         '\tassert(vmsr_num_slides == mappings_to_slide_cnt);')
    if a not in text:
        return text, False
    b = a.replace('\tif (kr != KERN_SUCCESS) {\n',
                  '\tif (kr != KERN_SUCCESS) {\n\t\tdroidvm_sr_vm_setup_kr = (int)kr;\n', 1)
    text = text.replace(a, b, 1)

    # 3: vm_shared_region_slide() failed
    a = '\t\tif (kr != KERN_SUCCESS) {\n\t\t\tSHARED_REGION_TRACE_ERROR(\n'
    if a not in text:
        return text, False
    b = ('\t\tif (kr != KERN_SUCCESS) {\n'
         '\t\t\tdroidvm_sr_vmstage = 3;\n'
         '\t\t\tdroidvm_sr_vm_slide_kr = (int)kr;\n'
         '\t\t\tdroidvm_sr_vm_slide_index = (int)i;\n'
         '\t\t\tSHARED_REGION_TRACE_ERROR(\n')
    text = text.replace(a, b, 1)

    i = text.find('int droidvm_sr_vmstage = 0;')
    print('    0007 insertion point:')
    for ln in text[i - 100:i + 480].splitlines()[:14]:
        print('      ' + ln)
    return text, True


def edit_008(text):
    """0008  osfmk/vm/vm_shared_region.c -- reuse an already-mapped shared region.

    run16: vmstage=1, vm_setup_kr=5, vm_slides=4, sr_first_mapping=0xc5e0000.
    setup() rolls sr_first_mapping back to -1 whenever a mapping fails
    (vm_shared_region_undo_mappings()), and vm_shared_region_map_file() does the
    same when slide fails.  A non-(-1) value therefore means the first
    __shared_region_map_and_slide_2_np() completed successfully.  dyld then
    calls it a second time; setup()'s "caught race in map and slide" guard
    returns KERN_FAILURE, the BSD tail maps that to EINVAL(22), and dyld
    concludes "no dyld cache" despite the cache being mapped.

    XNU already treats a mapped region as reusable (see
    vm_shared_region_start_address(): sr_first_mapping != -1 -> KERN_SUCCESS).
    Short-circuit before setup() instead of failing.  Returning success from
    inside setup() would be wrong: the caller then asserts
    vmsr_num_slides == mappings_to_slide_cnt on a DEVELOPMENT kernel.

    Keep all 0007 recorders; droidvm_sr_vmshort counts the short-circuits.
    """
    marker = 'int droidvm_sr_vm_slides = -1;\n'
    if marker not in text:
        return text, False
    text = text.replace(marker, marker + 'int droidvm_sr_vmshort = 0;\n', 1)

    anchor = ('\tdroidvm_sr_vmstage = 0;\n'
              '\tdroidvm_sr_vm_setup_kr = -1;\n'
              '\tdroidvm_sr_vm_slide_kr = -1;\n'
              '\tdroidvm_sr_vm_slide_index = -1;\n'
              '\tdroidvm_sr_vm_first_mapping = -2;\n')
    if anchor not in text:
        return text, False
    short = ('\t/* DroidVM 0008: the first call already installed every mapping.\n'
             '\t * dyld calls map_and_slide again here; rejecting it with\n'
             '\t * KERN_FAILURE becomes EINVAL and dyld drops the cache.\n'
             '\t * Reuse the existing region instead (same semantics as\n'
             '\t * vm_shared_region_start_address()). Return before setup()\n'
             '\t * so no lock / sr_mapping_in_progress state is held. */\n'
             '\tif (shared_region->sr_first_mapping != (mach_vm_offset_t)-1) {\n'
             '\t\tdroidvm_sr_vmshort++;\n'
             '\t\treturn KERN_SUCCESS;\n'
             '\t}\n\n')
    text = text.replace(anchor, short + anchor, 1)

    i = text.find('\t/* DroidVM 0008:')
    print('    0008 short-circuit:')
    for ln in text[i:i + 700].splitlines()[:16]:
        print('      ' + ln)
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

    print('0005 bsd/vm/vm_unix.c (the shared region result dyld sees)')
    path, text = read(tree, 'bsd/vm/vm_unix.c')
    if 'droidvm_sr_result' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_005(text)
        check('vm_unix anchors found', applied,
              'the _shared_region_map_and_slide anchors are missing: the patched source changed')
        check('recorder present', 'droidvm_sr_result = (int)kr;' in new, 'recorder did not land')
        check('counts present', 'droidvm_sr_mappings = (int)mappings_count;' in new, 'counts did not land')
        if applied:
            path.write_text(new)

    print('0006 bsd/vm/vm_unix.c (which shared-region check refused the cache)')
    path, text = read(tree, 'bsd/vm/vm_unix.c')
    if 'droidvm_sr_stage' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_006(text)
        check('setup anchors found', applied,
              'setup() definition / done label / vm_shared_region_map_file call changed')
        check('stage recorder present', 'droidvm_sr_stage = 1; goto done;' in new,
              'the goto-done instrumentation did not land')
        check('vmkr recorder present', 'droidvm_sr_vmkr = (int)kr;' in new,
              'the vm_shared_region_map_file recorder did not land')
        if applied:
            path.write_text(new)

    print('0007 osfmk/vm/vm_shared_region.c (which VM-side check returns KERN_FAILURE)')
    path, text = read(tree, 'osfmk/vm/vm_shared_region.c')
    if 'droidvm_sr_vmstage' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_007(text)
        check('vm anchors found', applied,
              'vm_shared_region_map_file_setup definition or one of the 0007 anchors changed')
        check('race recorder present', 'droidvm_sr_vmstage = 1;' in new,
              'the sr_first_mapping race recorder did not land')
        check('copyin recorder present', 'droidvm_sr_vmstage = 2;' in new,
              'the copyin KERN_FAILURE recorder did not land')
        check('slide recorder present', 'droidvm_sr_vmstage = 3;' in new,
              'the vm_shared_region_slide recorder did not land')
        check('setup kr recorder present', 'droidvm_sr_vm_setup_kr = (int)kr;' in new,
              'the setup kr recorder did not land')
        if applied:
            path.write_text(new)

    print('0008 osfmk/vm/vm_shared_region.c (reuse an already-mapped shared region)')
    path, text = read(tree, 'osfmk/vm/vm_shared_region.c')
    if 'droidvm_sr_vmshort' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_008(text)
        check('0007 recorders present', applied,
              'run 0007 first: the 0008 anchor is the 0007 reset block')
        check('short-circuit present',
              'shared_region->sr_first_mapping != (mach_vm_offset_t)-1' in new,
              'the reuse branch did not land')
        check('short-circuit counter present', 'droidvm_sr_vmshort++;' in new,
              'the short-circuit counter did not land')
        if applied:
            path.write_text(new)

    if FAILED:
        sys.exit('apply-xnu-changes: ' + ', '.join(FAILED))
    print('all changes are in the source the compiler will read')


if __name__ == '__main__':
    main()
