(()=>{
  'use strict';
  const $=(id)=>document.getElementById(id);
  const linkTab=$('tab-link'), uploadTab=$('tab-upload'),linkPanel=$('link-panel'),uploadPanel=$('upload-panel');
  const linkBtn=$('analyze-link'),uploadBtn=$('analyze-file'),fileInput=$('file-input'),drop=$('dropzone');
  const error=$('error'),status=$('status'),statusTitle=$('status-title'),statusSub=$('status-sub');
  let active='link',selectedFile=null,busy=false,pollTimer=null;
  $('year').textContent=new Date().getFullYear();
  const modeLabel=$('mode-label');
  fetch('/api/checker/health').then(r=>r.json()).then(x=>{modeLabel.textContent=x.model_enabled?'AI model connected':'Evidence-only mode';modeLabel.title=x.model_enabled?'Model detection enabled':'Configure Sightengine keys to enable visual AI predictions';}).catch(()=>{modeLabel.textContent='Checking service…'});
  function tab(type){if(busy)return; active=type;linkPanel.hidden=type!=='link';uploadPanel.hidden=type!=='upload';linkTab.classList.toggle('active',type==='link');uploadTab.classList.toggle('active',type==='upload');linkTab.setAttribute('aria-selected',type==='link');uploadTab.setAttribute('aria-selected',type==='upload');resetMessages();}
  linkTab.onclick=()=>tab('link');uploadTab.onclick=()=>tab('upload');
  function resetMessages(){error.hidden=true;status.hidden=true;}
  function showError(msg){status.hidden=true;error.textContent=msg||'Something went wrong. Try again.';error.hidden=false;}
  function setBusy(value){busy=value;linkBtn.disabled=value;uploadBtn.disabled=value;linkTab.disabled=value;uploadTab.disabled=value;}
  function showStatus(title,desc){statusTitle.textContent=title;statusSub.textContent=desc||'This might take a moment.';status.hidden=false;error.hidden=true;}
  function chooseFile(f){if(!f)return; if(!/\.(mp4|mov|m4v|webm|mkv)$/i.test(f.name)){showError('Select an MP4, MOV, M4V, MKV or WebM video.');return;}if(f.size>30*1024*1024){showError('This video exceeds the 30 MB upload limit.');return;}selectedFile=f;$('file-label').textContent=f.name;error.hidden=true;}
  drop.addEventListener('click',()=>fileInput.click());drop.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();fileInput.click()}});
  fileInput.onchange=()=>chooseFile(fileInput.files?.[0]);
  ['dragenter','dragover'].forEach(evt=>drop.addEventListener(evt,e=>{e.preventDefault();drop.classList.add('over')}));
  ['dragleave','drop'].forEach(evt=>drop.addEventListener(evt,e=>{e.preventDefault();drop.classList.remove('over')}));
  drop.addEventListener('drop',e=>chooseFile(e.dataTransfer?.files?.[0]));
  async function post(url,options){const r=await fetch(url,options);let data;try{data=await r.json()}catch{throw new Error('The server sent an unexpected response.')}if(!r.ok)throw new Error(data.detail||'Analysis failed.');return data;}
  async function start(mode){if(busy)return;resetMessages();let payload=null;
    if(mode==='link'){const url=$('url').value.trim();if(!url){showError('Paste a video link to begin.');return;}payload=()=>post('/api/checker/jobs/url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});}
    else{if(!selectedFile){showError('Choose a video file first.');return;}payload=()=>{let body=new FormData();body.append('video',selectedFile);return post('/api/checker/jobs/upload',{method:'POST',body})};}
    setBusy(true);$('report-section').hidden=true;showStatus(mode==='link'?'Retrieving the video…':'Uploading your video…','Please keep this tab open.');
    try{const j=await payload();await watch(j.id);}catch(e){showError(e.message||'Unable to analyze video.');setBusy(false)}
  }
  linkBtn.onclick=()=>start('link');uploadBtn.onclick=()=>start('upload');
  $('url').addEventListener('keydown',e=>{if(e.key==='Enter')start('link')});
  async function watch(id){let tries=0;
    const tick=async()=>{try{const r=await post('/api/checker/jobs/'+encodeURIComponent(id),{});
      if(r.status==='complete'){showReport(r.result);status.hidden=true;setBusy(false);return;}
      if(r.status==='error'){showError(r.error||'Could not analyze this clip.');setBusy(false);return;}
      tries++;if(tries>115){showError('Analysis is taking longer than expected. Please retry.');setBusy(false);return;}
      let label={queued:'Waiting in analysis queue',fetching:'Retrieving public video',processing:'Inspecting the video'}[r.status]||'Working on your report';
      showStatus(label+'…',r.phase||'Checking file and AI signals.');pollTimer=setTimeout(tick,2200);
    }catch(e){showError(e.message);setBusy(false)}};
    await tick();
  }
  function safeText(v){return typeof v==='string'?v:String(v??'')}
  function showReport(data){if(!data)return;const m=data.model||{};const statusClass=m.verdict==='likely_ai'?'positive':m.verdict==='likely_real'?'negative':'';
    const orb=$('report-orb');orb.className='report-orb '+statusClass;orb.textContent=m.verdict==='likely_ai'?'✳':m.verdict==='likely_real'?'✓':'?';
    $('report-category').textContent=data.provider?'Model-assisted evidence report':'Technical evidence report';
    $('verdict').textContent=safeText(m.title||'Inconclusive');$('report-description').textContent=safeText(m.description||'');
    $('score-panel').hidden=m.score==null;
    if(m.score!=null){$('score-value').textContent=Number(m.score).toFixed(0)+'%';$('score-fill').style.width=Math.max(0,Math.min(100,Number(m.score)))+'%'}
    const chart=$('frame-chart');chart.replaceChildren();const frames=m.frames||[];
    $('frame-count').textContent=frames.length?`${frames.length} sampled frames`:'';
    frames.forEach(x=>{const bar=document.createElement('div');bar.className='frame-bar';bar.title=`${x.position}s: ${Math.round(x.score*100)}% AI signal`;
      const inner=document.createElement('span');inner.style.height=`${Math.max(2,Math.round(x.score*100))}%`;bar.appendChild(inner);chart.appendChild(bar);});
    $('sample-note').textContent=(data.sample||'')+(data.model_error?' · '+data.model_error:'')+(data.provider?' · Scores are indicative, not proof.':' · Add an AI model provider to enable pixel-based classification.');
    const f=data.facts||{};$('video-facts').textContent=`${f.duration||'?'}s · ${f.width||'?'}×${f.height||'?'} · ${f.codec||'unknown'} · ${f.size_mb||'?'} MB`;
    $('provenance-text').textContent=safeText(data.provenance?.description||'Unknown');
    $('metadata-text').textContent=data.hints?.length?data.hints.map(x=>x.title+' (unverified)').join('; '):'No clear AI-generator labels in metadata. This does not imply a human-recorded video.';
    $('report-section').hidden=false;requestAnimationFrame(()=>$('report-section').scrollIntoView({behavior:'smooth',block:'start'}));
  }
  $('again').onclick=()=>{$('report-section').hidden=true;$('tool').scrollIntoView({behavior:'smooth',block:'center'});resetMessages()};
})();
