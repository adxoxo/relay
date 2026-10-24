// Pixtuoid sprite art: MIT, copyright 2026 Ivan Wang. See officeArt.LICENSE.txt.
import rawArt from "./officeArt.json";
import type { OfficeState } from "./officeState";
const art = rawArt as {
    palette: Record<string, string>;
    sprites: Record<string, string[][]>;
};
const W = 640, H = 408;
export const DESK_POSITIONS = [{ x: 130, y: 188 }, { x: 320, y: 188 }, { x: 510, y: 188 }, { x: 130, y: 310 }, { x: 320, y: 310 }, { x: 510, y: 310 }];
const SLOTS = DESK_POSITIONS;
// Small capped cache: environment changes cannot retain an unbounded number of rasters.
const cache = new Map<string, HTMLCanvasElement>();
function sprite(ctx: CanvasRenderingContext2D, name: string, x: number, y: number, scale = 1, overrides: Record<string, string> = {}) { const rows = art.sprites[name]; if (!rows)
    return; for (let iy = 0; iy < rows.length; iy++)
    for (let ix = 0; ix < rows[iy].length; ix++) {
        const key = rows[iy][ix], color = overrides[key] || art.palette[key];
        if (!color || color === 'transparent')
            continue;
        ctx.fillStyle = color;
        ctx.fillRect(Math.round(x + ix * scale), Math.round(y + iy * scale), Math.ceil(scale), Math.ceil(scale));
    } }
function rect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, color: string) { ctx.fillStyle = color; ctx.fillRect(x, y, w, h); }
function background(env: string) {
    const cached = cache.get(env);
    if (cached)
        return cached;
    const canvas = document.createElement('canvas');
    canvas.width = W;
    canvas.height = H;
    const c = canvas.getContext('2d');
    if (!c)
        return canvas;
    c.imageSmoothingEnabled = false;
    rect(c, 0, 0, W, H, '#26332e');
    rect(c, 0, 0, W, 112, '#303e36');
    rect(c, 0, 103, W, 9, '#1c2923');
    // Three windows put the same city behind each workspace, with a distinct evening palette.
    for (let win = 0; win < 3; win++) {
        const x = 246 + win * 125;
        rect(c, x - 4, 27, 111, 67, '#1d2928');
        rect(c, x, 31, 103, 59, env === 'work1' ? '#515e60' : '#535569');
        for (let i = 0; i < 7; i++) {
            const bx = x + i * 15, bh = 12 + ((i * 17 + win * 11) % 30);
            rect(c, bx, 90 - bh, 13, bh, env === 'work1' ? '#364944' : '#373b50');
            for (let wy = 90 - bh + 5; wy < 87; wy += 8)
                if ((i + wy + win) % 3)
                    rect(c, bx + 4, wy, 3, 3, '#b8ad73');
        }
        rect(c, x + 49, 31, 4, 59, '#25362d');
    }
    rect(c, 30, 48, 177, 42, '#172a25');
    rect(c, 33, 51, 171, 36, '#3b4c36');
    rect(c, 39, 56, 4, 25, '#c49d60');
    c.fillStyle = '#c6bf8e';
    c.font = '11px monospace';
    c.fillText(env === 'work1' ? 'MAKE GOOD THINGS.' : 'ONE THING AT A TIME.', 51, 73);
    rect(c, 0, 112, W, 296, '#75664d');
    for (let y = 112; y < H; y += 16) {
        rect(c, 0, y, W, 1, '#635b43');
        for (let x = (Math.floor(y / 16) % 2) * 38 - 76; x < W; x += 76) {
            rect(c, x, y, 1, 16, '#635b43');
            const v = (x * 13 + y * 7) % 5;
            if (v === 0)
                rect(c, x + 14, y + 7, 17, 1, '#887454');
            if (v === 2)
                rect(c, x + 24, y + 4, 11, 1, '#7f7051');
        }
    }
    rect(c, 0, 112, W, 3, '#a38c5d');
    rect(c, 0, H - 11, W, 11, '#303f32');
    rect(c, 0, H - 14, W, 3, '#b19865');
    // The aisle and furniture stay clear of all six desk targets.
    sprite(c, 'bookshelf@4x', 27, 107, .72);
    sprite(c, 'plant_tall@4x', 582, 94, .8);
    sprite(c, 'plant@4x', 20, 335, .85);
    sprite(c, 'plant@4x', 582, 337, .85);
    sprite(c, 'water_cooler@4x', 29, 237, .75);
    sprite(c, 'wall_clock@4x', 214, 46, .7);
    for (const slot of SLOTS) {
        rect(c, slot.x - 52, slot.y - 13, 104, 57, '#55604a');
        rect(c, slot.x - 48, slot.y - 10, 96, 50, '#60694f');
        sprite(c, 'desk_chair@4x', slot.x - 16, slot.y - 19, .95);
        sprite(c, 'desk_north@4x', slot.x - 31, slot.y + 1, 1.1);
    }
    if (cache.size >= 8)
        cache.delete(cache.keys().next().value!);
    cache.set(env, canvas);
    return canvas;
}
export interface OfficeSprite {
    id: string;
    slot: number;
    state: OfficeState;
}
function tint(id: string) { let value = 0; for (const ch of id)
    value = (value * 31 + ch.charCodeAt(0)) | 0; return (value >>> 0) % 4; }
export function paintOffice(canvas: HTMLCanvasElement, env: string, agents: OfficeSprite[], now: number) {
    const c = canvas.getContext("2d");
    if (!c)
        return;
    c.imageSmoothingEnabled = false;
    c.drawImage(background(env), 0, 0);
    for (const agent of agents) {
        const slot = DESK_POSITIONS[agent.slot];
        if (!slot)
            continue;
        const i = tint(agent.id), working = agent.state === "working", frame = Math.floor(now / 360) % 2;
        const skin = ["#edc39e", "#ba8c69", "#e7b48a", "#8c664b"][i];
        const shirt = (env === "work1" ? ["#cc925a", "#8b9a74", "#bd826d", "#73978e"] : ["#8998b7", "#91a58f", "#b29bba", "#a1a792"])[i];
        const hair = ["#322d24", "#5c4130", "#302b2a", "#745c39"][i];
        const body = agent.state === "idle" ? "seated_sleeping" : working ? "typing_" + frame : "seated";
        sprite(c, body, slot.x - 16, slot.y - 36, 4, { S: skin, B: shirt, H: hair, n: "#322d27" });
        sprite(c, "desk_north@4x", slot.x - 31, slot.y + 1, 1.1, { j: working ? "#344f40" : "#29332d", J: working ? "#9eb077" : "#566052" });
        if (working) {
            rect(c, slot.x - 14, slot.y + 12, 14 + frame * 6, 2, "#b1bf8b");
            rect(c, slot.x - 14, slot.y + 17, 21 - frame * 5, 2, "#82985f");
        }
        if (agent.state === "needs_input") {
            rect(c, slot.x - 20, slot.y + 8, 37, 17, "#9c7946");
            rect(c, slot.x - 17, slot.y + 12, 31, 2, "#ecd895");
            rect(c, slot.x - 17, slot.y + 17, 16, 2, "#e0bb72");
        }
    }
}
