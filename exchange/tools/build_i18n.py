import json,re,sys,pathlib
sys.path.insert(0,str(pathlib.Path(__file__).parent))
from dict import D,FULL
from errs import E
def rx(t):
    return "^"+"(.+?)".join(re.escape(p) for p in t.split("{}"))+"$"
data={"D":{k:list(v) for k,v in D.items()},"F":{k:list(v) for k,v in FULL.items()},"E":[[rx(e),a,f] for e,a,f in sorted(E,key=lambda x:-len(x[0].replace("{}","")))]}
rt=r"""
(()=>{
const DATA=__DATA__;
const AR_TZ=/^(Africa\/(Tunis|Algiers|Casablanca|El_Aaiun|Tripoli|Cairo|Khartoum|Nouakchott|Mogadishu)|Asia\/(Riyadh|Dubai|Qatar|Bahrain|Kuwait|Muscat|Aden|Baghdad|Amman|Beirut|Damascus|Gaza|Hebron)|Indian\/Comoro)$/;
const FR_TZ=/^(Europe\/(Paris|Brussels|Luxembourg|Monaco|Zurich|Busingen)|Africa\/(Dakar|Abidjan|Bamako|Ouagadougou|Niamey|Conakry|Lome|Porto-Novo|Douala|Libreville|Brazzaville|Kinshasa|Lubumbashi|Bangui|Ndjamena|Bujumbura|Kigali|Djibouti)|Indian\/(Antananarivo|Reunion|Mayotte)|America\/(Martinique|Guadeloupe|Cayenne|Port-au-Prince|Montreal|Miquelon)|Pacific\/(Tahiti|Noumea|Wallis))$/;
function detect(){let tz="";try{tz=Intl.DateTimeFormat().resolvedOptions().timeZone||""}catch(e){}
 if(AR_TZ.test(tz))return"ar";if(FR_TZ.test(tz))return"fr";
 if(/^(Africa|Asia|Europe|America|Australia|Pacific|Atlantic|Indian|Antarctica)\//.test(tz))return"en";
 const n=(navigator.language||"").slice(0,2);return n=="ar"?"ar":n=="fr"?"fr":"en"}
const L=["ar","fr","en"].includes(localStorage.lang)?localStorage.lang:detect();
window.LANG=L;
const de=document.documentElement;de.lang=L;de.dir=L=="ar"?"rtl":"ltr";
const I=L=="fr"?0:1,D=DATA.D,F=DATA.F,E=DATA.E.map(x=>[new RegExp(x[0]),x[1],x[2]]);
const RUN=/[\u0600-\u06FF«][\u0600-\u06FF\s،؟«»\-–\.:!0-9%\/\(\)]*[\u0600-\u06FF»\)\.؟!]|[\u0600-\u06FF]/g;
function words(r){const w=r.split(/\s+/),o=[];let i=0;while(i<w.length){let j=Math.min(w.length,i+60),hit=null;
 for(;j>i;j--){const k=w.slice(i,j).join(" ");if(D[k]){hit=D[k][I];break}}if(hit!=null){o.push(hit);i=j}else{o.push(w[i]);i++}}return o.join(" ")}
function ar2(s){const t=s.trim();if(F[t])return s.replace(t,F[t][I]);
 return s.replace(RUN,r=>{const k=r.replace(/\s+/g," ").trim();return D[k]?D[k][I]:words(k)})}
function srv(s){const t=s.trim();for(const [re,a,f] of E){const m=t.match(re);if(m){let i=1;return (L=="ar"?a:f).replace(/\{\}/g,()=>m[i++])}}return s}
const HAS=/[\u0600-\u06FF]/;
function T(s){if(s==null)return s;s=String(s);if(L=="ar")return srv(s);if(HAS.test(s))return ar2(s);return L=="fr"?srv(s):s}
window.T=T;
function node(n){if(n.nodeType==3){const v=n.nodeValue,x=(L=="ar"||!HAS.test(v))?v:ar2(v);if(x!==v)n.nodeValue=x;return}
 if(n.nodeType!=1||n.tagName=="SCRIPT"||n.tagName=="STYLE")return;
 for(const a of["placeholder","title"]){const v=n.getAttribute(a);if(v&&HAS.test(v)&&L!="ar"){const x=ar2(v);if(x!==v)n.setAttribute(a,x)}}
 if(n.tagName=="OPTION"||n.childNodes.length)for(const c of n.childNodes)node(c)}
function toastFix(){const t=document.getElementById("toast");if(t){const v=t.textContent,x=T(v);if(x!==v)t.textContent=x}}
const mo=new MutationObserver(ms=>{mo.disconnect();for(const m of ms){if(m.type=="characterData")node(m.target);else if(m.type=="attributes")node(m.target);else m.addedNodes.forEach(node)}toastFix();obs()});
const obs=()=>mo.observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:["placeholder","title"]});
const p0=window.prompt.bind(window),c0=window.confirm.bind(window),a0=window.alert.bind(window);
window.prompt=(m,d)=>p0(T(m),d);window.confirm=m=>c0(T(m));window.alert=m=>a0(T(m));
const NAMES={ar:"العربية",fr:"Français",en:"English"};
document.addEventListener("DOMContentLoaded",()=>{node(document.body);obs();document.title="ALTAF Exchange";
 const h=document.querySelector("header");if(h){const s=document.createElement("select");s.id="lang";s.setAttribute("aria-label","Language");
  s.innerHTML=Object.keys(NAMES).map(k=>`<option value="${k}"${k==L?" selected":""}>${NAMES[k]}</option>`).join("");
  s.onchange=()=>{localStorage.lang=s.value;location.reload()};h.appendChild(s)}});
})();
"""
out=pathlib.Path(__file__).parent.parent/"static"/"i18n.js"
out.write_text(rt.replace("__DATA__",json.dumps(data,ensure_ascii=False,separators=(",",":"))).strip()+"\n",encoding="utf-8")
print(out,out.stat().st_size)
