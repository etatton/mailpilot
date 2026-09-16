"""Generate the Windows version resource for the exe (CI build step).
An anonymous, metadata-free exe looks like malware to AV heuristics; a proper
company/product/version block is one of the signals that keeps Defender calm.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mailpilot import VERSION  # noqa: E402

nums = (VERSION.split("-")[0].split(".") + ["0", "0", "0"])[:4]
tup = ", ".join(nums + ["0"] * (4 - len(nums)))

out = Path(__file__).resolve().parent.parent / "build"
out.mkdir(exist_ok=True)
(out / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tup}),
    prodvers=({tup}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'ET3 Media'),
        StringStruct('FileDescription', 'MailPilot - Gmail reply copilot'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', 'MailPilot'),
        StringStruct('LegalCopyright', 'MIT License - ET3 Media'),
        StringStruct('OriginalFilename', 'MailPilot.exe'),
        StringStruct('ProductName', 'MailPilot'),
        StringStruct('ProductVersion', '{VERSION}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print("wrote", out / "version_info.txt", "for", VERSION)
