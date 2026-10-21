"""Prepare the MIT NVENC API header for DirectX-only hardware encoding.

No CUDA headers, libraries or dynamic loader are downloaded or used.
API 12.2 supports DirectX 12 and AV1 while retaining broad driver compatibility.
"""
from pathlib import Path
import hashlib
import json
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'native (dev)/external/VideoCodecHeaders-12.2'
COMMIT='c69278340ab1d5559c7d7bf0edf615dc33ddbba7'
BLOB='5dfec5207f3af11c8040b72b1f39008ff968cd92'
URL=f'https://raw.githubusercontent.com/FFmpeg/nv-codec-headers/{COMMIT}/include/ffnvcodec/nvEncodeAPI.h'


def main():
    target=DEST/'nvEncodeAPI.h'
    data=target.read_bytes() if target.exists() else b''
    identity=lambda value:hashlib.sha1(b'blob '+str(len(value)).encode()+b'\0'+value).hexdigest()
    if identity(data)!=BLOB:
        with urllib.request.urlopen(URL,timeout=60) as response:data=response.read()
    if identity(data)!=BLOB:raise RuntimeError('NVENC header does not match the pinned official Git blob')
    DEST.mkdir(parents=True,exist_ok=True)
    if not target.exists() or target.read_bytes()!=data:target.write_bytes(data)
    text=data.decode('utf-8-sig')
    license=text[text.index('/*')+2:text.index('*/')].strip()+'\n'
    (DEST/'LICENSE-header.txt').write_text(license,encoding='utf-8')
    (DEST/'source-manifest.json').write_text(json.dumps({'repository':'FFmpeg/nv-codec-headers',
        'tag':'n12.2.72.0','commit':COMMIT,'git_blob':BLOB,'sha256':hashlib.sha256(data).hexdigest(),
        'source':URL,'usage':'NVENC DirectX 12 hardware encoding; no CUDA'},indent=2)+'\n')
    print(f'Verified DirectX NVENC API header in {DEST}')


if __name__=='__main__':main()
