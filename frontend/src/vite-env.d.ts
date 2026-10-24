// Ambient module declarations for CSS side-effect imports.
// Vite handles these at build time; TypeScript needs the declaration.

declare module "*.css";
declare module "*.txt?url&no-inline" {
  const url: string;
  export default url;
}
