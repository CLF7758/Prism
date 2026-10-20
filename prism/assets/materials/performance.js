/* Shared low-volume WebEngine diagnostics, feeding Prism's operation log. */
'use strict';
window.prismPerformance=(()=>{
  let send=null,counts={},durations={},longTasks=0,longestTask=0;
  function count(name,value=1){counts[name]=(counts[name]||0)+value;}
  function span(name,start){const ms=performance.now()-start;
    const d=durations[name]||(durations[name]={count:0,total_ms:0,max_ms:0});
    d.count++;d.total_ms+=ms;d.max_ms=Math.max(d.max_ms,ms);
  }
  try{new PerformanceObserver(list=>{for(const entry of list.getEntries()){
    longTasks++;longestTask=Math.max(longestTask,entry.duration);
  }}).observe({entryTypes:['longtask']});}catch(_){}
  setInterval(()=>{
    if(!send||document.hidden)return;
    for(const d of Object.values(durations)){
      d.total_ms=Math.round(d.total_ms*100)/100;d.max_ms=Math.round(d.max_ms*100)/100;
    }
    send(JSON.stringify({counts,durations,long_tasks:longTasks,longest_task_ms:Math.round(longestTask),
      heap_bytes:performance.memory?.usedJSHeapSize||0}));
    counts={};durations={};longTasks=0;longestTask=0;
  },1000);
  return {count,span,connect(callback){send=callback;}};
})();
window.addEventListener('error',event=>{
  prismPerformance.count('script_errors');
});
window.addEventListener('unhandledrejection',event=>{
  prismPerformance.count('unhandled_promises');
});
