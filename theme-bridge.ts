// Typed access to backend/static/theme.js.
//
// theme.js is a classic script published by the sign-up page, where it keeps the
// browser's contrast maths identical to branding.py's. The Settings preview needs
// the same maths, and a second copy of it in TypeScript is exactly the drift the
// parity test exists to catch -- but tsconfig has no allowJs, so the module cannot
// be imported directly.
//
// So it is loaded as a classic script by react.html and read off the global. The
// fallback is deliberately a hard failure rather than a local implementation: if
// the script is missing, the preview says so instead of quietly disagreeing with
// the server about what counts as readable.

export type Palette={primary:string;secondary:string;accent:string};
export type Warning={ratio:number;minimum:number;message:string};

interface ThemeModule{
  FIELDS:readonly (keyof Palette)[];
  luminance(hex:string):number;
  contrast(a:string,b:string):number;
  textColor(background:string):string;
  paletteWarnings(palette:Palette):Warning[];
}

declare global{
  interface Window{ DuesdeskTheme?:ThemeModule }
}

function module():ThemeModule{
  const found=window.DuesdeskTheme;
  if(!found)throw new Error('theme.js did not load, so text contrast cannot be resolved.');
  return found;
}

export const textColor=(background:string)=>module().textColor(background);
export const paletteWarnings=(palette:Palette):Warning[]=>module().paletteWarnings(palette);

// The six variables, as inline style properties for a container. This is the same
// set branding_json publishes, so a preview inside the running application paints
// from exactly what the server would store.
export function themeStyle(palette:Palette):Record<string,string>{
  const style:Record<string,string>={};
  for(const key of ['primary','secondary','accent'] as const){
    style[`--org-${key}`]=palette[key];
    style[`--org-${key}-text`]=textColor(palette[key]);
  }
  return style;
}