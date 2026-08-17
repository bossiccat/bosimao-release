const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const workflowPath = path.join(repoRoot, ".github", "workflows", "android-gates.yml");
const fetchDepsPath = path.join(__dirname, "scripts", "fetch-deps.ps1");
const workflow = fs.readFileSync(workflowPath, "utf8");
const fetchDeps = fs.readFileSync(fetchDepsPath, "utf8");

assert.match(workflow, /Prepare sherpa-onnx dependencies/, "CI must prepare clean-runner sherpa dependencies");
assert.match(workflow, /scripts\\fetch-deps\.ps1/, "CI must run the checked-in dependency bootstrapper");
assert.match(workflow, /GRADLE_USER_HOME/, "CI must isolate Gradle user homes");
assert.match(workflow, /--project-cache-dir/, "CI must isolate project caches");
assert.match(workflow, /:app:compileDebugKotlin/, "CI must run compileDebugKotlin independently");
assert.match(workflow, /:app:testDebugUnitTest/, "CI must run testDebugUnitTest independently");
assert.match(workflow, /:app:assembleDebug/, "CI must run assembleDebug independently");
assert.match(workflow, /:app:assembleRelease/, "CI must run assembleRelease independently");
assert.match(workflow, /if:\s*\$\{\{ always\(\) \}\}/, "CI must retain artifacts and cleanup on failure");
assert.match(workflow, /gradlew\.bat --stop/, "CI must stop Gradle daemons in the always path");

assert.match(fetchDeps, /downloadTimeoutSec/, "bootstrapper must bound download duration");
assert.match(fetchDeps, /maxDownloadAttempts/, "bootstrapper must retry transient downloads");
assert.match(fetchDeps, /\.partial/, "bootstrapper must not accept partial files");
assert.match(fetchDeps, /Get-FileHash/, "bootstrapper must verify dependency hashes");
assert.match(fetchDeps, /Move-Item/, "bootstrapper must publish verified downloads atomically");

console.log("android-ci-contract: PASS");
