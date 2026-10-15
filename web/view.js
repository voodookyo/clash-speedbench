/* Shared rendering lifecycle. No measurement rules or credentials. */
(function(root){
  'use strict';
  function retainTable(tbody, render, doc){
    doc=doc||document;
    if(!tbody.querySelectorAll || !tbody.contains){render();return;}
    const active=doc.activeElement;
    const owned=active && tbody.contains(active);
    const row=owned && active.closest('tr[data-row-key]');
    const key=row && row.dataset.rowKey;
    const role=owned && active.tagName==='BUTTON'?active.className:null;
    const open=[...tbody.querySelectorAll('details[open]')].map(el=>{
      const parent=el.closest('tr.detail-row');
      return {key:parent && parent.previousElementSibling?.dataset.rowKey,
        label:el.querySelector('summary')?.textContent};
    });
    render();
    // Compare strings rather than interpolating untrusted IDs into selectors.
    for(const el of tbody.querySelectorAll('details')){
      const key=el.closest('tr.detail-row')?.previousElementSibling?.dataset.rowKey;
      if(open.some(x=>x.key===key && x.label===el.querySelector('summary')?.textContent)) el.open=true;
    }
    if(key){
      const next=[...tbody.querySelectorAll('tr[data-row-key]')].find(el=>el.dataset.rowKey===key);
      const focus=role && next?[...next.querySelectorAll('button')].find(el=>el.className===role):next;
      if(focus) focus.focus({preventScroll:true});
    }
  }
  const api={retainTable};
  if(typeof module!=='undefined' && module.exports) module.exports=api;
  root.SBView=api;
})(typeof globalThis!=='undefined'?globalThis:this);
