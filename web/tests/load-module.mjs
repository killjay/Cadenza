import fs from "node:fs/promises";
import vm from "node:vm";
import ts from "typescript";

// Run the actual TypeScript modules with transport/worker boundaries supplied
// by each test. No browser or native WASM kernel is required for these races.
export async function loadModule(path, dependencies = {}, globals = {}) {
  const context = vm.createContext({ console, ArrayBuffer, Uint8Array, DataView,
    TextEncoder, TextDecoder, Event, CustomEvent, performance, crypto, setTimeout,
    clearTimeout, ...globals });
  const source = await fs.readFile(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } });
  const module = new vm.SourceTextModule(outputText, { context });
  await module.link(async name => {
    if (!(name in dependencies)) throw new Error(`Unconfigured dependency: ${name}`);
    const exports = dependencies[name];
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

export function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
