'use strict';
const fs = require('node:fs');
const path = require('node:path');

const dir = 'C:/Users/Administrator/WorkBuddy/监视app/.worktrees/sidecar-native-publish-coordination/.lock-probe';
fs.mkdirSync(dir, { recursive: true });

for (let i = 1; i <= 20; i++) {
  const file = path.join(dir, `probe-${i}.lock`);
  try {
    const fd = fs.openSync(file, 'wx');
    fs.closeSync(fd);
    // immediate reopen — mimics cargo's create-then-access pattern
    const fd2 = fs.openSync(file, 'r+');
    fs.closeSync(fd2);
    console.log(`probe ${i}: OK`);
  } catch (error) {
    console.log(`probe ${i}: FAIL ${error.code}`);
  }
  if (i % 5 === 0) {
    try {
      const fd2 = fs.openSync(file, 'r+');
      fs.closeSync(fd2);
      console.log(`  delayed reopen ${i}: OK`);
    } catch (error) {
      console.log(`  delayed reopen ${i}: FAIL ${error.code}`);
    }
  }
}
console.log('done');
