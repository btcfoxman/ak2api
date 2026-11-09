import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';
const source=readFileSync(new URL('../app/static/app.js',import.meta.url),'utf8');
const body=source.slice(source.indexOf('function defaultPayload()'),source.indexOf('function renderModels('));
test('model form respects declared defaults rather than first allowed tier',()=>{
 const context={state:{selectedModel:'vidu-q4',models:[{id:'vidu-q4',capabilities:{durations:[3,4,5],resolutions:['540p','720p'],aspect_ratios:['16:9'],default_duration:5,default_resolution:'720p',default_aspect_ratio:'16:9',media_limits:{images:12,videos:0,audio:3}}}]}};
 vm.createContext(context);vm.runInContext(body,context);const payload=context.defaultPayload();
 assert.equal(payload.duration,5);assert.equal(payload.resolution,'720p');assert.equal(payload.aspect_ratio,'16:9');assert.ok(!('video_urls' in payload));
});
test('old models without explicit defaults retain existing first-tier fallback',()=>{
 const context={state:{selectedModel:'legacy',models:[{id:'legacy',capabilities:{durations:[4,5],resolutions:['480p','720p'],aspect_ratios:['adaptive'],media_limits:{images:9,videos:3,audio:3}}}]}};
 vm.createContext(context);vm.runInContext(body,context);const payload=context.defaultPayload();
 assert.equal(payload.duration,4);assert.equal(payload.resolution,'480p');assert.equal(payload.aspect_ratio,'adaptive');
});
