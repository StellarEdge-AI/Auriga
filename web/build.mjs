import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const { values } = parseArgs({ options: { outdir: { type: 'string', default: 'dist' } } });
const outputDirectory = resolve(sourceDirectory, values.outdir);
const result = await build({
  absWorkingDir: sourceDirectory,
  entryPoints: ['app.js', 'library.js'],
  bundle: true,
  minify: true,
  outdir: outputDirectory,
  write: false,
});

// Hash the exact delivered bytes, including CSS emitted from bundled imports.
const assets = new Map(result.outputFiles.map((file) => [basename(file.path), file.contents]));
for (const name of ['styles.css', 'library.css']) assets.set(name, await readFile(resolve(sourceDirectory, name)));
const versions = new Map([...assets].map(([name, contents]) => [name, createHash('sha256').update(contents).digest('hex').slice(0, 16)]));

function versionAssets(html) {
  return html.replace(/\b(src|href)=(['"])([^'"<>]+)\2/gi, (attribute, name, quote, reference) => {
    const url = new URL(reference, 'https://build.invalid/');
    if (url.origin !== 'https://build.invalid' || !/\.(?:js|css)$/i.test(url.pathname)) return attribute;
    const version = versions.get(basename(url.pathname));
    if (!version) throw new Error(`HTML references an asset that was not built: ${reference}`);
    url.searchParams.set('v', version);
    // Preserve /app.js for both the standalone gateway and the library's root
    // asset route; only the query changes. Source HTML remains untouched.
    const originalPath = reference.split(/[?#]/, 1)[0];
    return `${name}=${quote}${originalPath}${url.search}${url.hash}${quote}`;
  });
}

const pages = new Map();
for (const name of ['index.html', 'library.html']) {
  pages.set(name, versionAssets(await readFile(resolve(sourceDirectory, name), 'utf8')));
}
await mkdir(outputDirectory, { recursive: true });
await Promise.all([...assets, ...pages].map(([name, contents]) => writeFile(resolve(outputDirectory, name), contents)));
console.log(`Built ${assets.size} assets and ${pages.size} pages in ${outputDirectory}`);
