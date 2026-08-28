import struct

# Spine 3.8 binary .skel -> JSON (标准 Spine JSON 结构, 供 火影解包器.py 渲染)

TRANSFORM_MODES = ["normal", "onlyTranslation", "noRotationOrReflection", "noScale", "noScaleOrReflection"]
BLEND_MODES = ["normal", "additive", "multiply", "screen"]
POSITION_MODES = ["fixed", "percent"]
SPACING_MODES = ["length", "fixed", "percent"]
ROTATE_MODES = ["tangent", "chain", "chainScale"]
ATT_TYPES = ["region", "boundingbox", "mesh", "linkedmesh", "path", "point", "clipping"]

class BR:
    def __init__(self, d):
        self.d = d; self.p = 0; self.strings = []
    def byte(self):
        b = self.d[self.p]; self.p += 1; return b
    def boolean(self):
        return self.byte() != 0
    def varint(self, opt=True):
        b = self.byte(); r = b & 0x7F
        if b & 0x80:
            b = self.byte(); r |= (b & 0x7F) << 7
            if b & 0x80:
                b = self.byte(); r |= (b & 0x7F) << 14
                if b & 0x80:
                    b = self.byte(); r |= (b & 0x7F) << 21
                    if b & 0x80:
                        b = self.byte(); r |= (b & 0x7F) << 28
        if not opt:
            r = (r >> 1) ^ -(r & 1)
        return r
    def string(self):
        n = self.varint(True)
        if n == 0: return None
        if n == 1: return ""
        n -= 1
        s = self.d[self.p:self.p+n].decode("utf-8", "replace"); self.p += n
        return s
    def stringref(self):
        i = self.varint(True)
        return None if i == 0 else self.strings[i-1]
    def flt(self):
        v = struct.unpack_from(">f", self.d, self.p)[0]; self.p += 4; return v
    def int32(self):
        v = struct.unpack_from(">i", self.d, self.p)[0]; self.p += 4; return v
    def short(self):
        v = struct.unpack_from(">h", self.d, self.p)[0]; self.p += 2; return v
    def floats(self, n):
        v = struct.unpack_from(">%df" % n, self.d, self.p); self.p += 4*n; return list(v)
    def shorts(self, n):
        v = struct.unpack_from(">%dh" % n, self.d, self.p); self.p += 2*n; return list(v)

def rgba(c):
    return "%02x%02x%02x%02x" % ((c>>24)&255, (c>>16)&255, (c>>8)&255, c&255)
def rgb(c):
    return "%02x%02x%02x" % ((c>>16)&255, (c>>8)&255, c&255)

def read_curve(r):
    t = r.byte()
    if t == 1:
        return "stepped"
    if t == 2:
        return [r.flt(), r.flt(), r.flt(), r.flt()]
    return None

def read_vertices(r, n):
    # 返回 (vertices_flat, bones_flat)
    if not r.boolean():
        return r.floats(n*2), None
    bones = []; weights = []
    for _ in range(n):
        bc = r.varint(True)
        bones.append(bc)
        for _ in range(bc):
            bones.append(r.varint(True))
            weights.append(r.flt()); weights.append(r.flt()); weights.append(r.flt())
    return weights, bones

def interleave_weighted(weights, bones):
    out = []
    bi = 0; wi = 0
    while bi < len(bones):
        bc = bones[bi]; bi += 1
        out.append(bc)
        for _ in range(bc):
            out.append(bones[bi]); bi += 1
            out.append(weights[wi]); wi += 1
            out.append(weights[wi]); wi += 1
            out.append(weights[wi]); wi += 1
    return out

def parse_skel(data):
    r = BR(data)
    hsh = r.string(); ver = r.string()
    sx0 = r.flt(); sy0 = r.flt(); sw = r.flt(); sh = r.flt()
    nonessential = r.boolean()
    fps = 0; images = None; audio = None
    if nonessential:
        fps = r.flt(); images = r.string(); audio = r.string()
    n = r.varint(True)
    for _ in range(n):
        r.strings.append(r.string())

    bones = []
    nb = r.varint(True)
    for i in range(nb):
        name = r.string()
        parent = None if i == 0 else bones[r.varint(True)]["name"]
        bd = {"name": name, "parent": parent}
        bd["rotation"] = r.flt(); bd["x"] = r.flt(); bd["y"] = r.flt()
        bd["scaleX"] = r.flt(); bd["scaleY"] = r.flt(); bd["shearX"] = r.flt(); bd["shearY"] = r.flt()
        bd["length"] = r.flt()
        bd["transform"] = TRANSFORM_MODES[r.varint(True)]
        bd["skin"] = r.boolean()
        if nonessential: bd["color"] = rgba(r.int32())
        bones.append(bd)

    slots = []
    ns = r.varint(True)
    for _ in range(ns):
        name = r.string(); bidx = r.varint(True)
        sd = {"name": name, "bone": bones[bidx]["name"], "color": rgba(r.int32())}
        dark = r.int32()
        if dark != -1: sd["darkColor"] = rgb(dark)
        att = r.stringref()
        if att: sd["attachment"] = att
        sd["blend"] = BLEND_MODES[r.varint(True)]
        slots.append(sd)

    ik = []
    n = r.varint(True)
    for _ in range(n):
        name = r.string(); order = r.varint(True); skinreq = r.boolean()
        nn = r.varint(True)
        bs = [bones[r.varint(True)]["name"] for _ in range(nn)]
        target = bones[r.varint(True)]["name"]
        mix = r.flt(); soft = r.flt(); bend = r.byte()
        comp = r.boolean(); stretch = r.boolean(); uniform = r.boolean()
        ik.append({"name": name, "order": order, "bones": bs, "target": target, "bendPositive": bend >= 0,
                   "mix": mix, "softness": soft, "compress": comp, "stretch": stretch, "uniform": uniform})

    transform = []
    n = r.varint(True)
    for _ in range(n):
        name = r.string(); order = r.varint(True); skinreq = r.boolean()
        nn = r.varint(True)
        bs = [bones[r.varint(True)]["name"] for _ in range(nn)]
        target = bones[r.varint(True)]["name"]
        local = r.boolean(); relative = r.boolean()
        offrot = r.flt(); offx = r.flt(); offy = r.flt(); offsx = r.flt(); offsy = r.flt(); offshy = r.flt()
        rotmix = r.flt(); transmix = r.flt(); scalemix = r.flt(); shearmix = r.flt()
        transform.append({"name": name, "order": order, "bones": bs, "target": target, "local": local, "relative": relative,
                          "rotation": offrot, "x": offx, "y": offy, "scaleX": offsx, "scaleY": offsy, "shearY": offshy,
                          "rotateMix": rotmix, "translateMix": transmix, "scaleMix": scalemix, "shearMix": shearmix})

    path = []
    n = r.varint(True)
    for _ in range(n):
        name = r.string(); order = r.varint(True); skinreq = r.boolean()
        nn = r.varint(True)
        bs = [bones[r.varint(True)]["name"] for _ in range(nn)]
        target = slots[r.varint(True)]["name"]
        pm = POSITION_MODES[r.varint(True)]; sm = SPACING_MODES[r.varint(True)]; rm = ROTATE_MODES[r.varint(True)]
        offrot = r.flt(); pos = r.flt(); spc = r.flt(); rotmix = r.flt(); transmix = r.flt()
        path.append({"name": name, "order": order, "bones": bs, "target": target, "positionMode": pm, "spacingMode": sm,
                     "rotateMode": rm, "rotation": offrot, "position": pos, "spacing": spc,
                     "rotateMix": rotmix, "translateMix": transmix})

    skins = []
    linked = []
    def read_skin(default):
        if default:
            slotcnt = r.varint(True)
            if slotcnt == 0: return None
            name = "default"
        else:
            name = r.stringref()
            nb = r.varint(True)
            for _ in range(nb): r.varint(True)
            for _ in range(r.varint(True)): r.varint(True)
            for _ in range(r.varint(True)): r.varint(True)
            for _ in range(r.varint(True)): r.varint(True)
            slotcnt = r.varint(True)
        attachments = {}
        for _ in range(slotcnt):
            slotidx = r.varint(True)
            slotname = slots[slotidx]["name"]
            nn = r.varint(True)
            for _ in range(nn):
                attname = r.stringref()
                att = read_attachment(slotidx, attname)
                if att is not None:
                    attachments.setdefault(slotname, {})[attname] = att
        return {"name": name, "attachments": attachments}

    def read_attachment(slotidx, attname):
        name = r.stringref()
        if name is None: name = attname
        t = r.byte()
        if t == 0:  # region
            path = r.stringref(); rot = r.flt(); x = r.flt(); y = r.flt()
            scx = r.flt(); scy = r.flt(); w = r.flt(); h = r.flt(); color = r.int32()
            return {"type": "region", "name": name, "path": path or name, "x": x, "y": y,
                    "scaleX": scx, "scaleY": scy, "rotation": rot, "width": w, "height": h, "color": rgba(color)}
        if t == 2:  # mesh
            path = r.stringref(); color = r.int32(); vc = r.varint(True)
            uvs = r.floats(vc*2)
            tris = r.shorts(r.varint(True))
            verts, vbones = read_vertices(r, vc)
            hull = r.varint(True)
            edges = []; w = 0; h = 0
            if nonessential:
                edges = r.shorts(r.varint(True)); w = r.flt(); h = r.flt()
            att = {"type": "mesh", "name": name, "path": path or name, "uvs": uvs, "triangles": tris, "color": rgba(color)}
            if vbones is None:
                att["vertices"] = verts
            else:
                att["vertices"] = interleave_weighted(verts, vbones)
            att["hull"] = hull
            if nonessential: att["edges"] = edges; att["width"] = w; att["height"] = h
            return att
        if t == 3:  # linkedmesh
            path = r.stringref(); color = r.int32()
            skinname = r.stringref(); parent = r.stringref(); inherit = r.boolean()
            w = 0; h = 0
            if nonessential: w = r.flt(); h = r.flt()
            linked.append((len(skins)-1 if skins else 0, slotidx, attname, name, path or name, skinname, parent, inherit))
            return None  # 稍后解析
        if t == 1:  # boundingbox
            vc = r.varint(True); verts, vbones = read_vertices(r, vc)
            color = r.int32() if nonessential else 0
            return {"type": "boundingbox", "name": name, "vertexCount": vc}
        if t == 4:  # path
            closed = r.boolean(); cs = r.boolean(); vc = r.varint(True)
            verts, vbones = read_vertices(r, vc)
            for _ in range(vc//3): r.flt()
            color = r.int32() if nonessential else 0
            return {"type": "path", "name": name, "vertexCount": vc}
        if t == 5:  # point
            rot = r.flt(); x = r.flt(); y = r.flt()
            color = r.int32() if nonessential else 0
            return {"type": "point", "name": name, "x": x, "y": y, "rotation": rot}
        if t == 6:  # clipping
            endslot = r.varint(True); vc = r.varint(True)
            verts, vbones = read_vertices(r, vc)
            color = r.int32() if nonessential else 0
            return {"type": "clipping", "name": name, "vertexCount": vc}
        return None

    dskin = read_skin(True)
    if dskin is not None: skins.append(dskin)
    n = r.varint(True)
    for _ in range(n):
        s = read_skin(False)
        if s is not None: skins.append(s)

    # 解析 linkedmesh
    for (skinidx, slotidx, attname, name, path, skinname, parent, inherit) in linked:
        # 找 parent attachment
        src = None
        for sk in skins:
            if skinname is None or sk["name"] == skinname:
                src = sk["attachments"].get(slots[slotidx]["name"], {}).get(parent)
                if src is not None: break
        if src is None: continue
        att = dict(src)
        att["name"] = name; att["path"] = path
        att["type"] = "mesh"
        skins[skinidx]["attachments"].setdefault(slots[slotidx]["name"], {})[attname] = att

    events = []
    n = r.varint(True)
    for _ in range(n):
        name = r.stringref(); ival = r.varint(False); fval = r.flt()
        sval = r.string(); apath = r.string()
        if apath is not None: r.flt(); r.flt()
        events.append({"name": name, "int": ival, "float": fval, "string": sval})

    animations = {}
    na = r.varint(True)
    for _ in range(na):
        aname = r.string()
        anim = {"bones": {}, "slots": {}, "deform": {}, "transform": {}, "ik": {}}
        # slot timelines
        n = r.varint(True)
        for _ in range(n):
            slotidx = r.varint(True)
            slotname = slots[slotidx]["name"]
            nn = r.varint(True)
            for _ in range(nn):
                ttype = r.byte(); fc = r.varint(True)
                keys = []
                if ttype == 0:  # attachment
                    for _ in range(fc):
                        t = r.flt(); nm = r.stringref()
                        keys.append({"time": t, "name": nm})
                    anim["slots"].setdefault(slotname, {})["attachment"] = keys
                elif ttype == 1:  # color
                    for fi in range(fc):
                        t = r.flt(); c = rgba(r.int32())
                        k = {"time": t, "color": c}
                        if fi < fc-1:
                            cv = read_curve(r)
                            if cv is not None: k["curve"] = cv
                        keys.append(k)
                    anim["slots"].setdefault(slotname, {})["color"] = keys
                else:  # two color
                    for fi in range(fc):
                        t = r.flt(); c = rgba(r.int32()); dc = rgb(r.int32())
                        k = {"time": t, "color": c, "darkColor": dc}
                        if fi < fc-1:
                            cv = read_curve(r)
                            if cv is not None: k["curve"] = cv
                        keys.append(k)
                    anim["slots"].setdefault(slotname, {})["color"] = keys
        # bone timelines
        n = r.varint(True)
        for _ in range(n):
            bidx = r.varint(True); bname = bones[bidx]["name"]
            nn = r.varint(True)
            for _ in range(nn):
                ttype = r.byte(); fc = r.varint(True)
                keys = []
                for fi in range(fc):
                    t = r.flt()
                    if ttype == 0:  # rotate
                        k = {"time": t, "angle": r.flt()}
                        typ = "rotate"
                    else:
                        x = r.flt(); y = r.flt()
                        k = {"time": t, "x": x, "y": y}
                        typ = {1: "translate", 2: "scale", 3: "shear"}[ttype]
                    if fi < fc-1:
                        cv = read_curve(r)
                        if cv is not None: k["curve"] = cv
                    keys.append(k)
                anim["bones"].setdefault(bname, {})[typ] = keys
        # ik timelines
        n = r.varint(True)
        for _ in range(n):
            cidx = r.varint(True)
            cname = ik[cidx]["name"] if cidx < len(ik) else str(cidx)
            fc = r.varint(True)
            mixk = []; softk = []; bendk = []
            for fi in range(fc):
                t = r.flt(); mix = r.flt(); soft = r.flt(); bend = r.byte(); comp = r.boolean(); stretch = r.boolean()
                cv = None
                if fi < fc-1: cv = read_curve(r)
                def mk2(typ, val):
                    k = {"time": t, typ: val}
                    if cv is not None: k["curve"] = cv
                    return k
                mixk.append(mk2("mix", mix))
                softk.append(mk2("softness", soft))
                bendk.append(mk2("bendPositive", bend >= 0))
            anim["ik"].setdefault(cname, {})
            anim["ik"][cname]["mix"] = mixk
            anim["ik"][cname]["softness"] = softk
            anim["ik"][cname]["bendPositive"] = bendk
        # transform timelines
        n = r.varint(True)
        for _ in range(n):
            cidx = r.varint(True)
            cname = transform[cidx]["name"] if cidx < len(transform) else str(cidx)
            fc = r.varint(True)
            rotk = []; trak = []; scak = []; shek = []
            for fi in range(fc):
                t = r.flt(); rm = r.flt(); tm = r.flt(); sm = r.flt(); shm = r.flt()
                cv = None
                if fi < fc-1: cv = read_curve(r)
                def mk3(typ, val):
                    k = {"time": t, typ: val}
                    if cv is not None: k["curve"] = cv
                    return k
                rotk.append(mk3("rotateMix", rm))
                trak.append(mk3("translateMix", tm))
                scak.append(mk3("scaleMix", sm))
                shek.append(mk3("shearMix", shm))
            anim["transform"].setdefault(cname, {})
            anim["transform"][cname]["rotateMix"] = rotk
            anim["transform"][cname]["translateMix"] = trak
            anim["transform"][cname]["scaleMix"] = scak
            anim["transform"][cname]["shearMix"] = shek
        # path timelines (skip)
        n = r.varint(True)
        for _ in range(n):
            r.varint(True); nn = r.varint(True)
            for _ in range(nn):
                ttype = r.byte(); fc = r.varint(True)
                for fi in range(fc):
                    if ttype == 2:
                        r.flt(); r.flt(); r.flt()
                    else:
                        r.flt(); r.flt()
                    if fi < fc-1: read_curve(r)
        # deform timelines
        n = r.varint(True)
        for _ in range(n):
            skinidx = r.varint(True)
            skinname = skins[skinidx]["name"] if skinidx < len(skins) else "default"
            nn = r.varint(True)
            for _ in range(nn):
                slotidx = r.varint(True); slotname = slots[slotidx]["name"]
                nnn = r.varint(True)
                for _ in range(nnn):
                    attname = r.stringref()
                    fc = r.varint(True)
                    keys = []
                    for fi in range(fc):
                        t = r.flt(); end = r.varint(True)
                        k = {"time": t}
                        if end > 0:
                            start = r.varint(True); nvals = end
                            vals = [r.flt() for _ in range(nvals)]
                            k["offset"] = start; k["vertices"] = vals
                        if fi < fc-1:
                            cv = read_curve(r)
                            if cv is not None: k["curve"] = cv
                        keys.append(k)
                    anim["deform"].setdefault(skinname, {}).setdefault(slotname, {})[attname] = keys
        # draw order (skip)
        doc = r.varint(True)
        if doc > 0:
            for _ in range(doc):
                r.flt(); offc = r.varint(True)
                for _ in range(offc): r.varint(True); r.varint(True)
        # events (skip)
        ec = r.varint(True)
        if ec > 0:
            for _ in range(ec):
                r.flt(); r.varint(True); r.varint(False); r.flt()
                if r.boolean(): r.string()
        animations[aname] = anim

    return {
        "skeleton": {"spine": ver},
        "bones": bones, "slots": slots, "ik": ik, "transform": transform, "path": path,
        "skins": skins, "events": events, "animations": animations,
    }

if __name__ == "__main__":
    import sys, json
    for f in sys.argv[1:]:
        d = open(f, "rb").read()
        j = parse_skel(d)
        out = f + ".json"
        json.dump(j, open(out, "w", encoding="utf-8"), ensure_ascii=False)
        print(f, "->", out, "bones=%d slots=%d anims=%d skins=%d" % (len(j["bones"]), len(j["slots"]), len(j["animations"]), len(j["skins"])))
