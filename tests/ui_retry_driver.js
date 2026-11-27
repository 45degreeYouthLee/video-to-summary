// Execute the shipped page handlers against the Flask test server, with no browser dependencies.
const fs = require('fs');
const vm = require('vm');
const [pagePath, base, action, configJson, jobJson] = process.argv.slice(2);
const html = fs.readFileSync(pagePath, 'utf8');
function part(start, end) {
  const a = html.indexOf(start);
  const b = html.indexOf(end, a + start.length);
  if (a < 0 || b < 0) throw new Error('Page function not found: ' + start);
  return html.slice(a, b);
}
const ids = { retry: 'summaryRetryBtn', regenerate: 'menuRegen', create: 'btnSubmitSingle' };
const tag = html.match(new RegExp('<button\\b[^>]*\\bid="' + ids[action] + '"[^>]*>'))?.[0];
const click = tag?.match(/onclick="([^"]+)"/)?.[1];
if (!click) throw new Error('Button handler missing: ' + action);
const posts = [], toasts = [], responses = [];
const syntheticJob = JSON.parse(jobJson);
const context = vm.createContext({
  jobs: [syntheticJob], currentJobId: syntheticJob.id,
  currentConfig: JSON.parse(configJson), useMockData: false,
  showToast: (message) => toasts.push(message),
  fetchJobsFromApi: () => Promise.resolve(), closeMoreMenu: () => {},
  document: { getElementById: (id) => ({ newJobUrl: {value: 'https://video.invalid/BVNewSynthetic'}, forceAsr: {checked: false} })[id] },
  fetch: async (url, options) => {
    if (!url.startsWith('/api/jobs')) throw new Error('Unexpected request: ' + url);
    posts.push({url, method: options.method});
    const response = await fetch(base + url, options);
    responses.push({status: response.status, json: await response.clone().json()});
    return response;
  },
});
const pageFunctions = [
  part('function normalizeBiliUrl(url){', '\nfunction showToast('),
  part('async function apiJson(url, opts={}){', '\nasync function checkBackend('),
  part('async function regenerate(){', '\nasync function retryAsrCurrentJob('),
  part('async function submitNewJob(){', '\n// ====='),
  part('function validateApiConfig(forceAsr=false){', '\nfunction toggleApiKeyVisibility('),
].join('\n');
vm.runInContext(pageFunctions, context, {filename: pagePath});
(async () => {
  await vm.runInContext('(async () => { await ' + click + ' })()', context);
  process.stdout.write(JSON.stringify({click, posts, toasts, responses}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
