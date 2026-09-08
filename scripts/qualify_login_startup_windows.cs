// Windows-only UIA companion. It never switches the input desktop, sends input,
// accesses the registry. Default read mode never toggles the login checkbox;
// explicit enable/disable modes wait for the Python Run-state guard handshake.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;

class PrivateQtRead {
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateDesktop(string n,IntPtr d,IntPtr m,uint f,uint a,IntPtr s);
 [DllImport("user32.dll",SetLastError=true)] static extern bool SetThreadDesktop(IntPtr d);
 [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr d);
 [DllImport("user32.dll",SetLastError=true)] static extern IntPtr OpenInputDesktop(uint f,bool i,uint a);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h,int i,StringBuilder b,uint n,out uint needed);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h,StringBuilder text,int count);
 [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint thread);
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
 delegate bool EnumWindow(IntPtr window,IntPtr unused);
 [DllImport("user32.dll",ExactSpelling=true,SetLastError=true)] static extern bool EnumDesktopWindows(IntPtr desktop,EnumWindow callback,IntPtr unused);
 [DllImport("kernel32.dll",ExactSpelling=true)] static extern void SetLastError(uint code);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcess(string app,StringBuilder cmd,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref STARTUPINFO s,out PROCESS_INFORMATION p);
 [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr p,out long created,out long exited,out long kernel,out long user);
 [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int info,ref EXTENDED_LIMIT limit,uint length);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int info,ref JOB_ACCOUNTING accounting,uint length,IntPtr returned);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
 [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
 [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process,uint code);
 [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMIT {public long processTime,jobTime;public uint flags;public UIntPtr minWorkingSet,maxWorkingSet;public uint activeProcesses;public UIntPtr affinity;public uint priority,scheduling;}
 [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS {public ulong readOps,writeOps,otherOps,readBytes,writeBytes,otherBytes;}
 [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMIT {public BASIC_LIMIT basic;public IO_COUNTERS io;public UIntPtr processMemory,jobMemory,peakProcessMemory,peakJobMemory;}
 [StructLayout(LayoutKind.Sequential)] struct JOB_ACCOUNTING {public long totalUser,totalKernel,periodUser,periodKernel;public uint pageFaults,totalProcesses,activeProcesses,terminatedProcesses;}
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct STARTUPINFO {public int cb; public string reserved,desktop,title; public uint x,y,xSize,ySize,xCount,yCount,fill,flags;public short show,reserved2;public IntPtr reservedPtr,input,output,error;}
 [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION {public IntPtr process,thread;public uint pid,tid;}
 static string InputName(){IntPtr h=OpenInputDesktop(0,false,1);if(h==IntPtr.Zero)throw new Exception("input_desktop_unavailable");try{var s=new StringBuilder(512);uint n;if(!GetUserObjectInformation(h,2,s,1024,out n))throw new Exception("input_desktop_name");return s.ToString();}finally{CloseDesktop(h);}}
 static void Write(string root,string file,string text){string temporary=Path.Combine(root,file+".tmp");File.WriteAllText(temporary,text);File.Move(temporary,Path.Combine(root,file));}
 static void Stage(string root,string value){File.AppendAllText(Path.Combine(root,"uia-stages.txt"),DateTime.UtcNow.ToString("o")+" "+value+"\n");}
 [MTAThread] static int Main(string[] args) {
  try{return args[0]=="--inspect"?Inspect(args):Run(args);}catch(Exception error){try{string output=args[0]=="--inspect"?args[1]:args[2];if(Directory.Exists(output))File.WriteAllText(Path.Combine(output,args[0]=="--inspect"?"actor-fatal.txt":"helper-fatal.txt"),"error_type="+error.GetType().Name+"\n");}catch{}return 1;}
 }
 static int Inspect(string[] args) {
  string output=args[1],desktopName=args[4],action=args[5];uint expectedPid=UInt32.Parse(args[2]);long expectedCreated=Int64.Parse(args[3]);
  if(action!="read"&&action!="enable"&&action!="disable")throw new Exception("unknown_action");
  Stage(output,"actor_started");var name=new StringBuilder(512);uint needed;
  if(!GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()),2,name,1024,out needed)||name.ToString()!=desktopName||InputName()==desktopName)throw new Exception("actor_desktop_mismatch");
  Stage(output,"private_desktop_verified");IntPtr gui=OpenProcess(0x101000,false,expectedPid);if(gui==IntPtr.Zero)throw new Exception("owned_gui_unavailable");
  int retries=0,lastError=0;string navigationRole="",navigationPatterns="";bool navigationInvoked=false;
  try {
   long created,exited,kernel,user;if(!GetProcessTimes(gui,out created,out exited,out kernel,out user)||created!=expectedCreated)throw new Exception("owned_gui_identity_mismatch");
   var windows=new List<IntPtr>();EnumWindow collect=(window,unused)=>{uint pid;GetWindowThreadProcessId(window,out pid);if(pid==expectedPid)windows.Add(window);return true;};
   // .NET Framework's first callback-marshalling/native-binding call was observed
   // to leave error127 on an empty desktop. Warm that exact callback once, then
   // reset last-error and evaluate every measured enumeration normally.
   bool warm=EnumDesktopWindows(IntPtr.Zero,collect,IntPtr.Zero);int warmError=Marshal.GetLastWin32Error();Stage(output,"enumeration_binding_warmup_"+warm+"_"+warmError);
   DateTime deadline=DateTime.UtcNow.AddSeconds(80);
   while(DateTime.UtcNow<deadline&&!File.Exists(Path.Combine(output,"stop"))) {
    if(WaitForSingleObject(gui,0)==0)throw new Exception("owned_gui_exited");
    Stage(output,"enumerating");windows.Clear();
    SetLastError(0);if(!EnumDesktopWindows(IntPtr.Zero,collect,IntPtr.Zero)){retries++;lastError=Marshal.GetLastWin32Error();Stage(output,"enumeration_retry_"+lastError);if(lastError!=0&&lastError!=6)throw new Exception("desktop_enumeration_"+lastError);Thread.Sleep(250);continue;}
    foreach(IntPtr window in windows) {
     var title=new StringBuilder(256);GetWindowText(window,title,title.Capacity);if(title.ToString()!="CommunityAI")continue;
     Stage(output,"owned_window_matched");Stage(output,"element_from_handle");AutomationElement top=AutomationElement.FromHandle(window);
     Stage(output,"sharing_query");var sharing=top.FindFirst(TreeScope.Descendants,new AndCondition(new PropertyCondition(AutomationElement.NameProperty,"Sharing"),new OrCondition(new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.Button),new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.CheckBox),new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.RadioButton))));
     if(sharing!=null&&!navigationInvoked){Stage(output,"sharing_pattern");navigationRole=sharing.Current.ControlType.ProgrammaticName;var patterns=new List<string>();foreach(var available in sharing.GetSupportedPatterns())patterns.Add(available.ProgrammaticName);navigationPatterns=String.Join(",",patterns.ToArray());Stage(output,"sharing_role_"+navigationRole);Stage(output,"sharing_patterns_"+navigationPatterns);
      object invoke;if(!sharing.TryGetCurrentPattern(InvokePattern.Pattern,out invoke))throw new Exception("sharing_invoke_unavailable");Stage(output,"sharing_invoke");((InvokePattern)invoke).Invoke();navigationInvoked=true;}
     Stage(output,"checkbox_query");var boxes=top.FindAll(TreeScope.Descendants,new AndCondition(new PropertyCondition(AutomationElement.NameProperty,"Start CommunityAI when I sign in"),new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.CheckBox)));if(boxes.Count>1)throw new Exception("ambiguous_login_checkbox");var box=boxes.Count==1?boxes[0]:null;
     if(box==null)continue;Stage(output,"checkbox_pattern");object pattern;if(!box.TryGetCurrentPattern(TogglePattern.Pattern,out pattern))throw new Exception("checkbox_toggle_pattern_unavailable");
     Stage(output,"checkbox_state");string state=((TogglePattern)pattern).Current.ToggleState.ToString(),initial=state,type=box.Current.ControlType.ProgrammaticName;bool enabled=box.Current.IsEnabled;
     if(type!="ControlType.CheckBox")throw new Exception("unexpected_login_control_type");
     if(action!="read") {
      string before=action=="enable"?"Off":"On",after=action=="enable"?"On":"Off";
      if(!enabled||state!=before)throw new Exception("unexpected_initial_checkbox_state");
      Write(output,"action-ready.txt",action);Stage(output,"waiting_for_Run_guard");DateTime guardDeadline=DateTime.UtcNow.AddSeconds(15);
      while(!File.Exists(Path.Combine(output,"allow-action"))&&DateTime.UtcNow<guardDeadline)Thread.Sleep(50);
      if(!File.Exists(Path.Combine(output,"allow-action")))throw new Exception("Run_guard_deadline");
      if(((TogglePattern)pattern).Current.ToggleState.ToString()!=before)throw new Exception("checkbox_changed_before_action");
      Stage(output,"login_checkbox_"+action);((TogglePattern)pattern).Toggle();DateTime toggleDeadline=DateTime.UtcNow.AddSeconds(10);
      do{state=((TogglePattern)pattern).Current.ToggleState.ToString();if(state==after)break;Thread.Sleep(50);}while(DateTime.UtcNow<toggleDeadline);
      if(state!=after)throw new Exception("checkbox_action_not_applied");
     }
     Write(output,"uia.txt","result=passed\ncheckbox_name=Start CommunityAI when I sign in\ncontrol_type="+type+"\ninitial_state="+initial+"\nstate="+state+"\nenabled="+enabled+"\nregistry_mutation="+(action!="read")+"\nlogin_action="+action+"\nenumeration_retries="+retries+"\nlast_enumeration_error="+lastError+"\nactor_private_desktop_verified=True\nnavigation_control_type="+navigationRole+"\nnavigation_supported_patterns="+navigationPatterns+"\nnavigation_action=InvokePattern.Invoke\n");Stage(output,"read_complete");return 0;
    }Thread.Sleep(250);
   }throw new Exception("checkbox_read_deadline");
  }catch(Exception error){Stage(output,"failed_"+error.GetType().Name);Write(output,"actor-error.txt","error_type="+error.GetType().Name+"\nhresult="+error.HResult+"\n");return 1;}
  finally{CloseHandle(gui);}
 }
 static int Run(string[] args) {
  string executable=args[0],command=File.ReadAllText(args[1]),output=args[2],action=args.Length>3?args[3]:"read";
  if(action!="read"&&action!="enable"&&action!="disable")throw new Exception("unknown_action");
  string desktopName="CommunityAIReadOnly-"+Guid.NewGuid().ToString("N"),before=InputName();
  // Deliberately omit DESKTOP_SWITCHDESKTOP permission.
  IntPtr desktop=CreateDesktop(desktopName,IntPtr.Zero,IntPtr.Zero,0,0x00CB,IntPtr.Zero);
  if(desktop==IntPtr.Zero)throw new Exception("CreateDesktop_"+Marshal.GetLastWin32Error());
  PROCESS_INFORMATION process=new PROCESS_INFORMATION(),actor=new PROCESS_INFORMATION();bool noSwitch=true,closed=false,contained=false,actorContained=false;
  IntPtr job=IntPtr.Zero;uint activeAtClose=UInt32.MaxValue;bool jobClosed=false,jobCleanupVerified=false;
  string result="failed",error="";
  try {
   job=CreateJobObject(IntPtr.Zero,null);if(job==IntPtr.Zero)throw new Exception("CreateJobObject_"+Marshal.GetLastWin32Error());
   var limits=new EXTENDED_LIMIT();limits.basic.flags=0x2000;
   if(!SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(limits)))throw new Exception("job_kill_on_close_"+Marshal.GetLastWin32Error());
   var startup=new STARTUPINFO();startup.cb=Marshal.SizeOf(startup);startup.desktop="WinSta0\\"+desktopName;startup.flags=0x80;
   if(!CreateProcess(executable,new StringBuilder(command),IntPtr.Zero,IntPtr.Zero,false,0x08000004,IntPtr.Zero,output,ref startup,out process))throw new Exception("CreateProcess_"+Marshal.GetLastWin32Error());
   if(!AssignProcessToJobObject(job,process.process)){TerminateProcess(process.process,91);WaitForSingleObject(process.process,3000);throw new Exception("job_assignment_"+Marshal.GetLastWin32Error());}
   contained=true;
   long created,exited,kernel,user;if(!GetProcessTimes(process.process,out created,out exited,out kernel,out user))throw new Exception("process_creation_time");
   Write(output,"launch.txt",process.pid+"\n"+created+"\n"+desktopName+"\n"+before);
   if(ResumeThread(process.thread)==UInt32.MaxValue)throw new Exception("resume_thread_"+Marshal.GetLastWin32Error());
   string self=System.Reflection.Assembly.GetExecutingAssembly().Location;
   string actorCommand="\""+self+"\" --inspect \""+output+"\" "+process.pid+" "+created+" "+desktopName+" "+action;
   if(!CreateProcess(self,new StringBuilder(actorCommand),IntPtr.Zero,IntPtr.Zero,false,0x08000004,IntPtr.Zero,output,ref startup,out actor))throw new Exception("actor_create_process_"+Marshal.GetLastWin32Error());
   if(!AssignProcessToJobObject(job,actor.process)){TerminateProcess(actor.process,94);WaitForSingleObject(actor.process,3000);throw new Exception("actor_job_assignment");}actorContained=true;
   long actorCreated;if(!GetProcessTimes(actor.process,out actorCreated,out exited,out kernel,out user))throw new Exception("actor_creation_time");
   Write(output,"actor-launch.txt",actor.pid+"\n"+actorCreated+"\n"+desktopName);
   if(ResumeThread(actor.thread)==UInt32.MaxValue)throw new Exception("actor_resume_thread");
   DateTime until=DateTime.UtcNow.AddSeconds(115);
   while(DateTime.UtcNow<until&&!File.Exists(Path.Combine(output,"stop"))) {
    if(InputName()!=before){noSwitch=false;break;}
    if(WaitForSingleObject(actor.process,0)==0&&!File.Exists(Path.Combine(output,"uia.txt")))throw new Exception("actor_read_failed");
    Thread.Sleep(100);
   }
   if(!File.Exists(Path.Combine(output,"uia.txt")))throw new Exception("no_uia_result");
   if(!noSwitch)throw new Exception("input_desktop_changed");result="passed";
  }catch(Exception e){error=e.GetType().Name+":"+e.Message;}
  finally {
   // A zero count is required for normal acceptance. Closing a nonempty job is
   // emergency cleanup and must never turn a failed run into a passing result.
   if(job!=IntPtr.Zero){var accounting=new JOB_ACCOUNTING();if(QueryInformationJobObject(job,1,ref accounting,(uint)Marshal.SizeOf(accounting),IntPtr.Zero))activeAtClose=accounting.activeProcesses;else result="failed";
    jobCleanupVerified=activeAtClose==0;
    if(activeAtClose!=0){result="failed";TerminateJobObject(job,93);DateTime stopBy=DateTime.UtcNow.AddSeconds(3);
     do{if(QueryInformationJobObject(job,1,ref accounting,(uint)Marshal.SizeOf(accounting),IntPtr.Zero)&&accounting.activeProcesses==0){jobCleanupVerified=true;break;}Thread.Sleep(50);}while(DateTime.UtcNow<stopBy);}
    jobClosed=CloseHandle(job);if(!jobClosed)result="failed";}
   if(process.process!=IntPtr.Zero&&!contained){if(WaitForSingleObject(process.process,0)!=0)TerminateProcess(process.process,92);if(WaitForSingleObject(process.process,3000)!=0)jobCleanupVerified=false;result="failed";}
   if(actor.process!=IntPtr.Zero&&!actorContained){if(WaitForSingleObject(actor.process,0)!=0)TerminateProcess(actor.process,95);if(WaitForSingleObject(actor.process,3000)!=0)jobCleanupVerified=false;result="failed";}
   if(process.process!=IntPtr.Zero){CloseHandle(process.thread);CloseHandle(process.process);}
   if(actor.process!=IntPtr.Zero){CloseHandle(actor.thread);CloseHandle(actor.process);}
   closed=CloseDesktop(desktop);string after=InputName();noSwitch=noSwitch&&before==after;
   Write(output,"helper-result.txt","result="+result+"\nerror="+error+"\ninput_before="+before+"\ninput_after="+after+"\ninput_desktop_unchanged="+noSwitch+"\ndesktop_handle_closed="+closed+"\njob_assigned_before_resume="+contained+"\nactor_job_assigned_before_resume="+actorContained+"\njob_active_processes_at_close="+activeAtClose+"\njob_handle_closed="+jobClosed+"\njob_cleanup_verified="+jobCleanupVerified+"\n");
  }return result=="passed"&&noSwitch&&closed?0:1;
 }
}
