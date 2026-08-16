const fs = require('fs');
const path = require('path');
const assert = require('assert');

const workflowPath = path.join(__dirname, '..', '.github', 'workflows', 'android-gates.yml');
const workflow = fs.readFileSync(workflowPath, 'utf8');

assert.match(workflow, /name: Prepare sherpa-onnx dependencies/);
assert.match(workflow, /scripts\\fetch-deps\.ps1/);
assert.match(workflow, /sherpa-onnx-1\.13\.4\.aar/);
assert.match(workflow, /tokens\.txt/);
assert.match(workflow, /\$required = @\(/);
assert.match(workflow, /"app\/libs\/sherpa-onnx-1\.13\.4\.aar"/);
assert.match(workflow, /"\$modelDir\/tokens\.txt"/);
assert.match(workflow, /foreach \(\$file in \$required\)/);
assert.match(workflow, /Test-Path -LiteralPath \$file -PathType Leaf/);
assert.match(workflow, /exit 1/);
console.log('android-ci-deps-contract: PASS');
