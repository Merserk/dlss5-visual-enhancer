"""Vendor-neutral GLSL implementations of CAS and NIS sharpen-only kernels.

CAS: Copyright (C) 2024 Advanced Micro Devices, Inc.
NIS: Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES.
See src/desktop/AMD-FidelityFX-CAS-LICENSE.txt and NVIDIA-NIS-LICENSE.txt.
"""
from __future__ import annotations

import numpy as np

from .filters import filter_array, shader_filter


def sharpening_filter(method: str, sharpness: int, transfer: str = "srgb", *, depth: int = 16) -> str:
    if isinstance(sharpness, bool) or not isinstance(sharpness, int) or not 0 <= sharpness <= 100:
        raise ValueError("Sharpness must be an integer from 0 to 100.")
    if transfer not in {"srgb", "bt709", "linear"}:
        raise ValueError("Unsupported sharpening transfer function.")
    source = """//!HOOK MAIN
//!BIND HOOKED
vec4 pixel(ivec2 d) {
    vec2 p = clamp(floor(HOOKED_pos * HOOKED_size) + vec2(d), vec2(0.0), HOOKED_size - 1.0);
    return HOOKED_tex((p + 0.5) / HOOKED_size);
}
"""
    if depth == 8:
        source = source.replace("return HOOKED_tex((p + 0.5) / HOOKED_size);", "return round(HOOKED_tex((p + 0.5) / HOOKED_size) * 255.0) / 255.0;")
    if method == "AMD CAS":
        if transfer == "srgb":
            source += "vec3 lin(vec3 c) { return mix(pow((c+0.055)/1.055,vec3(2.4)),c/12.92,lessThanEqual(c,vec3(0.04045))); }\nvec3 enc(vec3 c) { return mix(1.055*pow(c,vec3(1.0/2.4))-0.055,c*12.92,lessThanEqual(c,vec3(0.0031308))); }\n"
        elif transfer == "bt709":
            source += "vec3 lin(vec3 c) { return mix(pow((c+0.099)/1.099,vec3(1.0/0.45)),c/4.5,lessThan(c,vec3(0.081))); }\nvec3 enc(vec3 c) { return mix(1.099*pow(c,vec3(0.45))-0.099,c*4.5,lessThan(c,vec3(0.018))); }\n"
        else:
            source += "vec3 lin(vec3 c) { return c; } vec3 enc(vec3 c) { return c; }\n"
        source += f"""vec4 hook() {{
    vec4 b=pixel(ivec2(0,-1)),d=pixel(ivec2(-1,0)),e=pixel(ivec2(0)),f=pixel(ivec2(1,0)),h=pixel(ivec2(0,1));
    if(min(min(b.a,d.a),min(min(e.a,f.a),h.a)) < 0.99999) return e;
    vec3 B=lin(b.rgb),D=lin(d.rgb),E=lin(e.rgb),F=lin(f.rgb),H=lin(h.rgb);
    float mn=min(min(min(D.g,E.g),F.g),min(B.g,H.g));
    float mx=max(max(max(D.g,E.g),F.g),max(B.g,H.g));
    float w=sqrt(clamp(min(mn,1.0-mx)/max(mx,1e-20),0.0,1.0))*{-1.0/(8.0-3.0*sharpness/100.0):.10f};
    vec3 result=clamp((E+w*(B+D+F+H))/(1.0+4.0*w),0.0,1.0);
    return vec4(clamp(enc(mix(E,result,{sharpness/100.0:.8f})),0.0,1.0),e.a);
}}
"""
    elif method == "NVIDIA NIS":
        slider = sharpness / 100.0 - 0.5
        positive = slider >= 0.0
        strength_min = max(0.0, 0.4 + slider * (1.25 if positive else 1.0) * 1.2)
        strength_max = 1.6 + slider * (1.25 if positive else 1.75) * 1.8
        limit_min = max(0.1, 0.14 + slider * (1.25 if positive else 1.0) * 0.32)
        limit_max = 0.5 + slider * (1.25 if positive else 1.0) * 0.6
        source += """float usm(float a,float b,float c,float d,float e,float strength,float limit) {
    float ac=max(max(a,b),c)-min(min(a,b),c),bc=max(max(c,d),e)-min(min(c,d),e);
    float ratio=max(ac,bc)/(min(ac,bc)+1.0/255.0);
    float lti=1.0-clamp((ratio-2.0)/8.0,0.0,1.0);
    return clamp((-0.6001*b+1.2002*c-0.6001*d)*strength,-limit,limit)*lti;
}
vec4 hook() {
    vec4 center=pixel(ivec2(0)); float p[25];
    for(int y=0;y<5;y++) for(int x=0;x<5;x++) {
        vec4 c=pixel(ivec2(x-2,y-2));
        if(c.a<0.99999) return center;
        p[y*5+x]=dot(c.rgb,vec3(0.2126,0.7152,0.0722));
    }
    float g0=abs(p[6]+p[7]+p[8]-p[16]-p[17]-p[18]);
    float g45=abs(p[11]+p[6]+p[7]-p[17]-p[18]-p[13]);
    float g90=abs(p[6]+p[11]+p[16]-p[8]-p[13]-p[18]);
    float g135=abs(p[11]+p[16]+p[17]-p[7]-p[8]-p[13]);
    float ax=max(g0,g90),an=min(g0,g90),dx=max(g45,g135),dn=min(g45,g135);
    bool ad=ax>an*(2.0*1127.0/1024.0)&&ax>64.0/1024.0&&ax>dn;
    bool dd=dx>dn*(2.0*1127.0/1024.0)&&dx>64.0/1024.0&&dx>an;
    float af=ad&&dd?ax/max(ax+dx,1e-20):1.0,df=ad&&dd?dx/max(ax+dx,1e-20):1.0;
"""
        source += f"""    float scale=1.0-clamp((p[12]-0.45)/(0.9-0.45),0.0,1.0);
    float strength=scale*{strength_max-strength_min:.10f}+{strength_min:.10f};
    float limit=(scale*{limit_max-limit_min:.10f}+{limit_min:.10f})*p[12];
    float sum=0.0;
    if(ad) sum+=af*(g0>=g90?usm(p[2],p[7],p[12],p[17],p[22],strength,limit):usm(p[10],p[11],p[12],p[13],p[14],strength,limit));
    if(dd) sum+=df*(g45>=g135?usm(p[6],0.5*(p[11]+p[7]),p[12],0.5*(p[17]+p[13]),p[18],strength,limit):usm(p[16],0.5*(p[17]+p[11]),p[12],0.5*(p[13]+p[7]),p[8],strength,limit));
    return vec4(clamp(center.rgb+sum,0.0,1.0),center.a);
}}
"""
    else:
        raise ValueError(f"Unknown sharpening method: {method!r}.")
    return shader_filter(source)


def sharpen_rgb(pixels: np.ndarray, sharpness: int, *, method: str = "AMD CAS",
                transfer: str = "srgb", alpha: np.ndarray | None = None, controller=None) -> np.ndarray:
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype not in (np.uint8, np.uint16):
        raise ValueError("Sharpening requires uint8/uint16 RGB pixels.")
    if alpha is not None and (alpha.shape != pixels.shape[:2] or alpha.dtype != pixels.dtype):
        raise ValueError("Sharpening alpha must match RGB pixels.")
    graph = sharpening_filter(method, sharpness, transfer, depth=8 if pixels.dtype == np.uint8 else 16)
    if sharpness == 0 or min(pixels.shape[:2]) < 2:
        return pixels.copy()
    rgba = np.empty((*pixels.shape[:2], 4), dtype=pixels.dtype)
    rgba[..., :3] = pixels
    rgba[..., 3] = np.iinfo(pixels.dtype).max if alpha is None else alpha
    result = filter_array(rgba, graph, controller=controller)[..., :3]
    if alpha is not None:
        result[alpha == 0] = pixels[alpha == 0]
    return np.ascontiguousarray(result)
