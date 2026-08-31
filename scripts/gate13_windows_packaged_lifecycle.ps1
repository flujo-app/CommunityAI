# Gate 13 native Windows clean-host packaged lifecycle adapter.
#
# Stage this script beside gate13_windows_localhost_inference.ps1,
# gate13_packaged_lifecycle.py, gate13-windows-run.json, payload/, and audit/.
# Run with Windows PowerShell 5.1 as an ordinary user. It accepts no arguments.
#
# All packaged processes are created suspended, assigned to a kill-on-close Job
# Object, and only then resumed. Control/API keys remain in this PowerShell
# process's memory and are never put in argv, environment, files, transcripts,
# diagnostics, or evidence.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$DebugPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"

. (Join-Path $PSScriptRoot "gate13_windows_localhost_inference.ps1")

$script:LifecycleArchive = Join-Path $PSScriptRoot "payload\communityai-desktop-windows.zip"
$script:LifecycleAuditRoot = Join-Path $PSScriptRoot "audit"
$script:LifecycleRunInput = Join-Path $PSScriptRoot "gate13-windows-run.json"
$script:LifecycleController = Join-Path $PSScriptRoot "gate13_packaged_lifecycle.py"
$script:LifecycleWorkRoot = Join-Path $PSScriptRoot "gate13-windows-work"
$script:LifecycleInstallRoot = Join-Path $script:LifecycleWorkRoot "install"
$script:LifecycleProductRoot = Join-Path $script:LifecycleInstallRoot "CommunityAI"
$script:LifecycleDesktopExe = Join-Path $script:LifecycleProductRoot "CommunityAI.exe"
$script:LifecycleNodeExe = Join-Path $script:LifecycleProductRoot "node\CommunityAI-Node.exe"
$script:LifecycleBootstrap = Join-Path $script:LifecycleProductRoot "_internal\bootstrap\catalog-bootstrap.json"
$script:LifecyclePersistentRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".drift\node"
$script:LifecycleNodeConfig = Join-Path $script:LifecyclePersistentRoot "node-config.json"
$script:LifecycleMaxJsonInputBytes = 2 * 1024 * 1024
$script:LifecycleMaxOutputBytes = 1048576
$script:LifecycleProcess = $null
$script:LifecycleAcquisitionInvoked = $false
$script:LifecycleOwnWorkRoot = $false
$script:LifecycleOwnPersistentRoot = $false

function Initialize-Gate13NativeHost {
    if ($null -ne ("Gate13.NativeHost" -as [type])) {
        return
    }

    Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace Gate13
{
    public sealed class NativeRunResult
    {
        public int ExitCode { get; private set; }
        public string Output { get; private set; }

        internal NativeRunResult(int exitCode, string output)
        {
            ExitCode = exitCode;
            Output = output;
        }
    }

    public sealed class ContainedProcess : IDisposable
    {
        private IntPtr job;
        private IntPtr process;
        private UInt32 processId;
        private bool closed;

        internal ContainedProcess(IntPtr jobHandle, IntPtr processHandle, UInt32 rootProcessId)
        {
            job = jobHandle;
            process = processHandle;
            processId = rootProcessId;
        }

        public int ActiveProcessCount
        {
            get
            {
                if (closed || job == IntPtr.Zero)
                {
                    return 0;
                }
                return NativeHost.QueryActiveProcesses(job);
            }
        }

        public bool RootExited
        {
            get
            {
                return process == IntPtr.Zero ||
                    NativeHost.WaitForSingleObject(process, 0) == NativeHost.WaitObject0;
            }
        }

        private void ReleaseHandles()
        {
            if (process != IntPtr.Zero)
            {
                NativeHost.CloseHandle(process);
                process = IntPtr.Zero;
            }
            if (job != IntPtr.Zero)
            {
                NativeHost.CloseHandle(job);
                job = IntPtr.Zero;
            }
            processId = 0;
            closed = true;
        }

        private bool WaitForEmpty(int timeoutMilliseconds)
        {
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
            while (job != IntPtr.Zero && NativeHost.QueryActiveProcesses(job) != 0 &&
                   DateTime.UtcNow < deadline)
            {
                Thread.Sleep(20);
            }
            return job == IntPtr.Zero || NativeHost.QueryActiveProcesses(job) == 0;
        }

        public bool StopGracefully(int timeoutMilliseconds)
        {
            if (closed)
            {
                return true;
            }
            if (timeoutMilliseconds < 0 || timeoutMilliseconds > 300000)
            {
                throw new ArgumentOutOfRangeException("timeoutMilliseconds");
            }

            bool closeRequested = true;
            try
            {
                NativeHost.RequestWindowClose(processId);
            }
            catch
            {
                closeRequested = false;
            }
            if (closeRequested && WaitForEmpty(timeoutMilliseconds))
            {
                ReleaseHandles();
                return true;
            }

            ForceAndVerify(30000);
            return false;
        }

        public void ForceAndVerify(int timeoutMilliseconds)
        {
            if (closed)
            {
                return;
            }
            if (timeoutMilliseconds < 0 || timeoutMilliseconds > 300000)
            {
                throw new ArgumentOutOfRangeException("timeoutMilliseconds");
            }

            Exception failure = null;
            try
            {
                if (job != IntPtr.Zero && !NativeHost.TerminateJobObject(job, 209))
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error != NativeHost.ErrorAccessDenied)
                    {
                        failure = new Win32Exception(error);
                    }
                }
                if (!WaitForEmpty(timeoutMilliseconds))
                {
                    failure = new InvalidOperationException("contained process tree survived");
                }
            }
            finally
            {
                ReleaseHandles();
            }
            if (failure != null)
            {
                throw failure;
            }
        }

        public void Dispose()
        {
            ForceAndVerify(30000);
        }
    }

    public static class NativeHost
    {
        internal const UInt32 WaitObject0 = 0;
        internal const int ErrorAccessDenied = 5;
        private const UInt32 Infinite = 0xffffffff;
        private const UInt32 CreateSuspended = 0x00000004;
        private const UInt32 CreateUnicodeEnvironment = 0x00000400;
        private const UInt32 CreateNoWindow = 0x08000000;
        private const UInt32 StartfUseStdHandles = 0x00000100;
        private const UInt32 HandleFlagInherit = 0x00000001;
        private const UInt32 JobObjectExtendedLimitInformation = 9;
        private const UInt32 JobObjectBasicAccountingInformation = 1;
        private const UInt32 JobObjectLimitKillOnJobClose = 0x00002000;
        private const UInt32 StdInputHandle = 0xfffffff6;

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public UInt32 Length;
            public IntPtr SecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)]
            public bool InheritHandle;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public UInt32 cb;
            public string reserved;
            public string desktop;
            public string title;
            public UInt32 x;
            public UInt32 y;
            public UInt32 xSize;
            public UInt32 ySize;
            public UInt32 xCountChars;
            public UInt32 yCountChars;
            public UInt32 fillAttribute;
            public UInt32 flags;
            public UInt16 showWindow;
            public UInt16 reserved2;
            public IntPtr reserved2Pointer;
            public IntPtr standardInput;
            public IntPtr standardOutput;
            public IntPtr standardError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation
        {
            public IntPtr process;
            public IntPtr thread;
            public UInt32 processId;
            public UInt32 threadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public UInt64 ReadOperationCount;
            public UInt64 WriteOperationCount;
            public UInt64 OtherOperationCount;
            public UInt64 ReadTransferCount;
            public UInt64 WriteTransferCount;
            public UInt64 OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimitInformation
        {
            public Int64 PerProcessUserTimeLimit;
            public Int64 PerJobUserTimeLimit;
            public UInt32 LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public UInt32 ActiveProcessLimit;
            public UIntPtr Affinity;
            public UInt32 PriorityClass;
            public UInt32 SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimitInformation
        {
            public BasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicAccountingInformation
        {
            public Int64 TotalUserTime;
            public Int64 TotalKernelTime;
            public Int64 ThisPeriodTotalUserTime;
            public Int64 ThisPeriodTotalKernelTime;
            public UInt32 TotalPageFaultCount;
            public UInt32 TotalProcesses;
            public UInt32 ActiveProcesses;
            public UInt32 TotalTerminatedProcesses;
        }

        private sealed class Spawned
        {
            public IntPtr Job;
            public IntPtr Process;
            public IntPtr ReadPipe;
            public IntPtr InputWrite;
        }

        internal delegate bool EnumWindowsCallback(IntPtr window, IntPtr parameter);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr parameter);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern UInt32 GetWindowThreadProcessId(IntPtr window, out UInt32 processId);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool PostMessage(IntPtr window, UInt32 message, IntPtr wParam, IntPtr lParam);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            UInt32 informationClass,
            IntPtr information,
            UInt32 informationLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool QueryInformationJobObject(
            IntPtr job,
            UInt32 informationClass,
            IntPtr information,
            UInt32 informationLength,
            out UInt32 returnLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool TerminateJobObject(IntPtr job, UInt32 exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out UInt32 exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern UInt32 GetProcessId(IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern UInt32 ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CreatePipe(
            out IntPtr readPipe,
            out IntPtr writePipe,
            ref SecurityAttributes attributes,
            UInt32 size
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetHandleInformation(
            IntPtr handle,
            UInt32 mask,
            UInt32 flags
        );

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetStdHandle(UInt32 standardHandle);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
            UInt32 creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref StartupInfo startupInfo,
            out ProcessInformation processInformation
        );

        private static string Quote(string value)
        {
            if (value == null)
            {
                throw new ArgumentNullException("value");
            }
            if (value.Length == 0)
            {
                return "\"\"";
            }
            bool needsQuotes = false;
            foreach (char character in value)
            {
                if (Char.IsWhiteSpace(character) || character == '"')
                {
                    needsQuotes = true;
                    break;
                }
            }
            if (!needsQuotes)
            {
                return value;
            }

            StringBuilder result = new StringBuilder();
            result.Append('"');
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\')
                {
                    slashes++;
                    continue;
                }
                if (character == '"')
                {
                    result.Append('\\', slashes * 2 + 1);
                    result.Append('"');
                    slashes = 0;
                    continue;
                }
                result.Append('\\', slashes);
                slashes = 0;
                result.Append(character);
            }
            result.Append('\\', slashes * 2);
            result.Append('"');
            return result.ToString();
        }

        private static StringBuilder BuildCommandLine(string executable, string[] arguments)
        {
            StringBuilder command = new StringBuilder(Quote(executable));
            if (arguments != null)
            {
                foreach (string argument in arguments)
                {
                    command.Append(' ');
                    command.Append(Quote(argument));
                }
            }
            return command;
        }

        private static IntPtr CreateKillOnCloseJob()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            IntPtr buffer = IntPtr.Zero;
            try
            {
                ExtendedLimitInformation limits = new ExtendedLimitInformation();
                limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
                int size = Marshal.SizeOf(typeof(ExtendedLimitInformation));
                buffer = Marshal.AllocHGlobal(size);
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    buffer,
                    checked((UInt32)size)
                ))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                return job;
            }
            catch
            {
                CloseHandle(job);
                throw;
            }
            finally
            {
                if (buffer != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(buffer);
                }
            }
        }

        internal static void RequestWindowClose(UInt32 processId)
        {
            const UInt32 WindowClose = 0x0010;
            if (processId == 0)
            {
                throw new InvalidOperationException("root process identity unavailable");
            }
            int matched = 0;
            if (!EnumWindows(delegate(IntPtr window, IntPtr parameter)
            {
                UInt32 owner;
                GetWindowThreadProcessId(window, out owner);
                if (owner == processId)
                {
                    matched++;
                    PostMessage(window, WindowClose, IntPtr.Zero, IntPtr.Zero);
                }
                return true;
            }, IntPtr.Zero))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            if (matched == 0)
            {
                throw new InvalidOperationException("packaged GUI window unavailable");
            }
        }

        internal static bool TerminateAndWait(IntPtr job, UInt32 exitCode, int timeoutMilliseconds)
        {
            if (!TerminateJobObject(job, exitCode))
            {
                int error = Marshal.GetLastWin32Error();
                if (error != ErrorAccessDenied)
                {
                    return false;
                }
            }
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
            while (QueryActiveProcesses(job) != 0 && DateTime.UtcNow < deadline)
            {
                Thread.Sleep(20);
            }
            return QueryActiveProcesses(job) == 0;
        }

        internal static int QueryActiveProcesses(IntPtr job)
        {
            IntPtr buffer = IntPtr.Zero;
            try
            {
                int size = Marshal.SizeOf(typeof(BasicAccountingInformation));
                buffer = Marshal.AllocHGlobal(size);
                UInt32 returned;
                if (!QueryInformationJobObject(
                    job,
                    JobObjectBasicAccountingInformation,
                    buffer,
                    checked((UInt32)size),
                    out returned
                ))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                BasicAccountingInformation accounting =
                    (BasicAccountingInformation)Marshal.PtrToStructure(
                        buffer,
                        typeof(BasicAccountingInformation)
                    );
                return checked((int)accounting.ActiveProcesses);
            }
            finally
            {
                if (buffer != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(buffer);
                }
            }
        }

        private static Spawned Spawn(
            string executable,
            string[] arguments,
            string currentDirectory,
            bool capture,
            bool redirectInput
        )
        {
            if (String.IsNullOrWhiteSpace(executable) || !Path.IsPathRooted(executable) ||
                !File.Exists(executable))
            {
                throw new InvalidOperationException("executable rejected");
            }
            if (String.IsNullOrWhiteSpace(currentDirectory) || !Path.IsPathRooted(currentDirectory) ||
                !Directory.Exists(currentDirectory))
            {
                throw new InvalidOperationException("working directory rejected");
            }

            IntPtr job = IntPtr.Zero;
            IntPtr readPipe = IntPtr.Zero;
            IntPtr writePipe = IntPtr.Zero;
            IntPtr inputRead = IntPtr.Zero;
            IntPtr inputWrite = IntPtr.Zero;
            ProcessInformation process = new ProcessInformation();
            try
            {
                job = CreateKillOnCloseJob();
                StartupInfo startup = new StartupInfo();
                startup.cb = checked((UInt32)Marshal.SizeOf(typeof(StartupInfo)));
                bool inheritHandles = false;
                UInt32 flags = CreateSuspended | CreateUnicodeEnvironment;
                if (capture)
                {
                    SecurityAttributes pipeAttributes = new SecurityAttributes();
                    pipeAttributes.Length = checked((UInt32)Marshal.SizeOf(typeof(SecurityAttributes)));
                    pipeAttributes.InheritHandle = true;
                    if (!CreatePipe(out readPipe, out writePipe, ref pipeAttributes, 0))
                    {
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    if (!SetHandleInformation(readPipe, HandleFlagInherit, 0))
                    {
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    startup.flags = StartfUseStdHandles;
                    startup.standardInput = GetStdHandle(StdInputHandle);
                    if (redirectInput)
                    {
                        if (!CreatePipe(out inputRead, out inputWrite, ref pipeAttributes, 0))
                        {
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        if (!SetHandleInformation(inputWrite, HandleFlagInherit, 0))
                        {
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        startup.standardInput = inputRead;
                    }
                    startup.standardOutput = writePipe;
                    startup.standardError = writePipe;
                    inheritHandles = true;
                    flags |= CreateNoWindow;
                }

                if (!CreateProcess(
                    executable,
                    BuildCommandLine(executable, arguments),
                    IntPtr.Zero,
                    IntPtr.Zero,
                    inheritHandles,
                    flags,
                    IntPtr.Zero,
                    currentDirectory,
                    ref startup,
                    out process
                ))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (!AssignProcessToJobObject(job, process.process))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (ResumeThread(process.thread) == UInt32.MaxValue)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                CloseHandle(process.thread);
                process.thread = IntPtr.Zero;
                if (writePipe != IntPtr.Zero)
                {
                    CloseHandle(writePipe);
                    writePipe = IntPtr.Zero;
                }
                if (inputRead != IntPtr.Zero)
                {
                    CloseHandle(inputRead);
                    inputRead = IntPtr.Zero;
                }
                return new Spawned {
                    Job = job,
                    Process = process.process,
                    ReadPipe = readPipe,
                    InputWrite = inputWrite
                };
            }
            catch
            {
                if (process.process != IntPtr.Zero)
                {
                    if (job != IntPtr.Zero)
                    {
                        TerminateJobObject(job, 210);
                    }
                    CloseHandle(process.process);
                }
                if (process.thread != IntPtr.Zero)
                {
                    CloseHandle(process.thread);
                }
                if (readPipe != IntPtr.Zero)
                {
                    CloseHandle(readPipe);
                }
                if (writePipe != IntPtr.Zero)
                {
                    CloseHandle(writePipe);
                }
                if (inputRead != IntPtr.Zero)
                {
                    CloseHandle(inputRead);
                }
                if (inputWrite != IntPtr.Zero)
                {
                    CloseHandle(inputWrite);
                }
                if (job != IntPtr.Zero)
                {
                    CloseHandle(job);
                }
                throw;
            }
        }

        public static ContainedProcess Start(
            string executable,
            string[] arguments,
            string currentDirectory
        )
        {
            Spawned spawned = Spawn(executable, arguments, currentDirectory, false, false);
            return new ContainedProcess(
                spawned.Job,
                spawned.Process,
                GetProcessId(spawned.Process)
            );
        }

        public static NativeRunResult Run(
            string executable,
            string[] arguments,
            string currentDirectory,
            int timeoutMilliseconds,
            int maximumOutputBytes
        )
        {
            return RunInternal(
                executable,
                arguments,
                currentDirectory,
                timeoutMilliseconds,
                maximumOutputBytes,
                null
            );
        }

        public static NativeRunResult RunWithInput(
            string executable,
            string[] arguments,
            string currentDirectory,
            int timeoutMilliseconds,
            int maximumOutputBytes,
            string input
        )
        {
            if (input == null || Encoding.UTF8.GetByteCount(input) > 1048576)
            {
                throw new ArgumentException("input rejected");
            }
            return RunInternal(
                executable,
                arguments,
                currentDirectory,
                timeoutMilliseconds,
                maximumOutputBytes,
                input
            );
        }

        private static NativeRunResult RunInternal(
            string executable,
            string[] arguments,
            string currentDirectory,
            int timeoutMilliseconds,
            int maximumOutputBytes,
            string input
        )
        {
            if (timeoutMilliseconds < 1 || timeoutMilliseconds > 7200000 ||
                maximumOutputBytes < 1 || maximumOutputBytes > 1048576)
            {
                throw new ArgumentOutOfRangeException();
            }
            Spawned spawned = Spawn(
                executable,
                arguments,
                currentDirectory,
                true,
                input != null
            );
            if (input != null)
            {
                try
                {
                    SafeFileHandle safeInput = new SafeFileHandle(spawned.InputWrite, true);
                    using (FileStream inputStream = new FileStream(safeInput, FileAccess.Write, 4096, false))
                    {
                        byte[] inputBytes = new UTF8Encoding(false, true).GetBytes(input);
                        try
                        {
                            inputStream.Write(inputBytes, 0, inputBytes.Length);
                            inputStream.Flush();
                        }
                        finally
                        {
                            Array.Clear(inputBytes, 0, inputBytes.Length);
                        }
                    }
                    spawned.InputWrite = IntPtr.Zero;
                }
                catch
                {
                    TerminateAndWait(spawned.Job, 214, 30000);
                    if (spawned.InputWrite != IntPtr.Zero)
                    {
                        CloseHandle(spawned.InputWrite);
                    }
                    CloseHandle(spawned.ReadPipe);
                    CloseHandle(spawned.Process);
                    CloseHandle(spawned.Job);
                    throw;
                }
            }
            SafeFileHandle safeRead = new SafeFileHandle(spawned.ReadPipe, true);
            FileStream stream = new FileStream(safeRead, FileAccess.Read, 4096, false);
            Decoder decoder = new UTF8Encoding(false, true).GetDecoder();
            StringBuilder output = new StringBuilder();
            bool overflow = false;
            Exception readFailure = null;
            Thread reader = new Thread(delegate()
            {
                byte[] bytes = new byte[8192];
                char[] chars = new char[8192];
                try
                {
                    int count;
                    while ((count = stream.Read(bytes, 0, bytes.Length)) != 0)
                    {
                        int charCount = decoder.GetChars(bytes, 0, count, chars, 0, false);
                        if (!overflow &&
                            Encoding.UTF8.GetByteCount(output.ToString()) +
                            Encoding.UTF8.GetByteCount(chars, 0, charCount) <= maximumOutputBytes)
                        {
                            output.Append(chars, 0, charCount);
                        }
                        else
                        {
                            overflow = true;
                            output.Length = 0;
                        }
                    }
                    decoder.GetChars(new byte[0], 0, 0, chars, 0, true);
                }
                catch (Exception error)
                {
                    readFailure = error;
                }
            });
            reader.IsBackground = true;
            reader.Start();

            Exception failure = null;
            int exit = -1;
            try
            {
                UInt32 wait = WaitForSingleObject(
                    spawned.Process,
                    checked((UInt32)timeoutMilliseconds)
                );
                if (wait != WaitObject0)
                {
                    bool cleaned = TerminateAndWait(spawned.Job, 211, 30000);
                    failure = cleaned
                        ? (Exception)new TimeoutException("contained process timed out")
                        : new InvalidOperationException("timed out process tree survived cleanup");
                }
                else
                {
                    UInt32 rawExit;
                    if (!GetExitCodeProcess(spawned.Process, out rawExit))
                    {
                        failure = new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    else
                    {
                        exit = unchecked((int)rawExit);
                    }
                }
                if (!reader.Join(30000))
                {
                    bool cleaned = TerminateAndWait(spawned.Job, 212, 30000);
                    failure = cleaned
                        ? (Exception)new InvalidOperationException("output drain timed out")
                        : new InvalidOperationException("output process tree survived cleanup");
                    reader.Join(30000);
                }
                if (readFailure != null)
                {
                    failure = readFailure;
                }
                if (overflow)
                {
                    failure = new InvalidOperationException("process output exceeded bound");
                }
                DateTime deadline = DateTime.UtcNow.AddSeconds(30);
                while (QueryActiveProcesses(spawned.Job) != 0 && DateTime.UtcNow < deadline)
                {
                    Thread.Sleep(20);
                }
                if (QueryActiveProcesses(spawned.Job) != 0)
                {
                    bool cleaned = TerminateAndWait(spawned.Job, 213, 30000);
                    failure = cleaned
                        ? (Exception)new InvalidOperationException("contained descendants required forced cleanup")
                        : new InvalidOperationException("contained descendants survived cleanup");
                }
            }
            finally
            {
                stream.Dispose();
                CloseHandle(spawned.Process);
                CloseHandle(spawned.Job);
            }
            if (failure != null)
            {
                throw failure;
            }
            return new NativeRunResult(exit, output.ToString());
        }
    }

    public static class NativeCredentialCount
    {
        private const UInt32 CredTypeGeneric = 1;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Credential
        {
            public UInt32 Flags;
            public UInt32 Type;
            public IntPtr TargetName;
            public IntPtr Comment;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
            public UInt32 CredentialBlobSize;
            public IntPtr CredentialBlob;
            public UInt32 Persist;
            public UInt32 AttributeCount;
            public IntPtr Attributes;
            public IntPtr TargetAlias;
            public IntPtr UserName;
        }

        [DllImport("Advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredRead(
            string target,
            UInt32 type,
            UInt32 flags,
            out IntPtr credentialPointer
        );

        [DllImport("Advapi32.dll", SetLastError = false)]
        private static extern void CredFree(IntPtr credentialPointer);

        private static int ExactCount(string target, string account, bool usernameMismatchMeansMissing)
        {
            IntPtr pointer;
            if (!CredRead(target, CredTypeGeneric, 0, out pointer) || pointer == IntPtr.Zero)
            {
                return 0;
            }
            try
            {
                Credential credential =
                    (Credential)Marshal.PtrToStructure(pointer, typeof(Credential));
                string actualTarget = Marshal.PtrToStringUni(credential.TargetName);
                string userName = Marshal.PtrToStringUni(credential.UserName);
                if (credential.Type != CredTypeGeneric ||
                    !String.Equals(actualTarget, target, StringComparison.Ordinal))
                {
                    throw new InvalidOperationException("credential identity mismatch");
                }
                if (!String.Equals(userName, account, StringComparison.Ordinal))
                {
                    if (usernameMismatchMeansMissing)
                    {
                        return 0;
                    }
                    throw new InvalidOperationException("credential identity mismatch");
                }
                return 1;
            }
            finally
            {
                CredFree(pointer);
            }
        }

        public static int Count(string service, string account)
        {
            return ExactCount(service, account, true) +
                ExactCount(account + "@" + service, account, false);
        }
    }
}
"@ -Language CSharp -ErrorAction Stop | Out-Null
}

function Invoke-Gate13Contained {
    param(
        [Parameter(Mandatory = $true)] [string] $Executable,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $WorkingDirectory,
        [int] $TimeoutSeconds = 180
    )
    Initialize-Gate13NativeHost
    $result = [Gate13.NativeHost]::Run(
        $Executable,
        $Arguments,
        $WorkingDirectory,
        [int]($TimeoutSeconds * 1000),
        $script:LifecycleMaxOutputBytes
    )
    if ($result.ExitCode -ne 0) {
        throw "packaged command failed"
    }
    return $result.Output
}

function Start-Gate13Product {
    if ($null -ne $script:LifecycleProcess) {
        throw "product already running"
    }
    Initialize-Gate13NativeHost
    $script:LifecycleProcess = [Gate13.NativeHost]::Start(
        $script:LifecycleDesktopExe,
        [string[]]@(),
        $script:LifecycleProductRoot
    )
}

function Stop-Gate13Product {
    if ($null -eq $script:LifecycleProcess) {
        return
    }
    $owned = $script:LifecycleProcess
    $script:LifecycleProcess = $null
    $graceful = $owned.StopGracefully(60000)
    if (-not $graceful -or $owned.ActiveProcessCount -ne 0) {
        throw "graceful product cleanup failed"
    }
}

function Stop-Gate13ProductForFault {
    if ($null -eq $script:LifecycleProcess) {
        throw "fault target unavailable"
    }
    $owned = $script:LifecycleProcess
    $script:LifecycleProcess = $null
    $owned.ForceAndVerify(30000)
    if ($owned.ActiveProcessCount -ne 0) {
        throw "fault cleanup failed"
    }
}

function Force-Gate13ProductCleanup {
    if ($null -eq $script:LifecycleProcess) {
        return
    }
    $owned = $script:LifecycleProcess
    $script:LifecycleProcess = $null
    $owned.ForceAndVerify(30000)
    if ($owned.ActiveProcessCount -ne 0) {
        throw "product cleanup failed"
    }
}

function Get-Gate13CredentialCount {
    Initialize-Gate13NativeHost
    $count = [Gate13.NativeCredentialCount]::Count(
        $script:CredentialService,
        $script:CredentialAccount
    )
    if ($count -gt 1) {
        throw "credential state ambiguous"
    }
    return $count
}

function Measure-Gate13Phase {
    param(
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [scriptblock] $Action
    )
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $facts = & $Action
    $timer.Stop()
    if ($null -eq $facts -or -not ($facts -is [System.Collections.IDictionary])) {
        throw "phase facts rejected"
    }
    $duration = [Math]::Round($timer.Elapsed.TotalSeconds, 6)
    if ([double]::IsNaN($duration) -or [double]::IsInfinity($duration) -or
        $duration -lt 0 -or $duration -gt 86400) {
        throw "phase duration rejected"
    }
    $record = [ordered]@{
        phase = $Name
        passed = $true
        duration_seconds = $duration
    }
    foreach ($key in $facts.Keys) {
        if ($record.Contains($key)) {
            throw "duplicate phase fact"
        }
        $record[$key] = $facts[$key]
    }
    return $record
}

function Get-Gate13FileCount {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return 0
    }
    return @(
        Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction Stop
    ).Count
}

function Get-Gate13DirectoryBytes {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [int64]0
    }
    [int64]$total = 0
    foreach ($item in Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction Stop) {
        $total = [int64]($total + [int64]$item.Length)
    }
    return $total
}

function Get-Gate13Sha256 {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "required file missing"
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Read-Gate13JsonFile {
    param([Parameter(Mandatory = $true)] [string] $Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Length -lt 2 -or $item.Length -gt $script:LifecycleMaxJsonInputBytes) {
        throw "JSON input rejected"
    }
    $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
    try {
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        return ConvertFrom-Gate13Json -Payload $utf8.GetString($bytes)
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Remove-Gate13ExactTree {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        throw "cleanup path rejected"
    }
    $full = [System.IO.Path]::GetFullPath($Path)
    $profile = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath("UserProfile"))
    $qualification = [System.IO.Path]::GetFullPath($PSScriptRoot)
    $allowed = (
        $full -ceq [System.IO.Path]::GetFullPath($script:LifecycleInstallRoot) -or
        $full -ceq [System.IO.Path]::GetFullPath($script:LifecycleWorkRoot) -or
        $full -ceq [System.IO.Path]::GetFullPath($script:LifecyclePersistentRoot)
    )
    if (-not $allowed -or $full -ceq $profile -or $full -ceq $qualification) {
        throw "cleanup path rejected"
    }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
    }
}


function Assert-Gate13String {
    param(
        [Parameter(Mandatory = $true)] [object] $Value,
        [Parameter(Mandatory = $true)] [string] $Pattern
    )
    if (-not ($Value -is [string]) -or $Value -notmatch $Pattern) {
        throw "string value rejected"
    }
}

function Test-Gate13SafeWindowsPathSegment {
    param([Parameter(Mandatory = $true)] [string] $Segment)
    if (
        [string]::IsNullOrEmpty($Segment) -or
        $Segment -ceq "." -or
        $Segment -ceq ".." -or
        $Segment.EndsWith(" ", [StringComparison]::Ordinal) -or
        $Segment.EndsWith(".", [StringComparison]::Ordinal) -or
        $Segment.IndexOfAny([char[]]'<>:"|?*') -ge 0
    ) {
        return $false
    }
    foreach ($character in $Segment.ToCharArray()) {
        if ([int]$character -lt 32) {
            return $false
        }
    }
    $stem = $Segment.Split(".")[0]
    if (
        $stem.Equals('CONIN$', [StringComparison]::OrdinalIgnoreCase) -or
        $stem.Equals('CONOUT$', [StringComparison]::OrdinalIgnoreCase)
    ) {
        return $false
    }
    if ($stem -match "^(?i:CON|PRN|AUX|NUL|COM[1-9\u00B9\u00B2\u00B3]|LPT[1-9\u00B9\u00B2\u00B3])$") {
        return $false
    }
    return $true
}

function Test-Gate13SafeArchivePath {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (
        [string]::IsNullOrWhiteSpace($Path) -or
        $Path.Contains("\") -or
        $Path.StartsWith("/") -or
        $Path.Contains(":") -or
        $Path.EndsWith("//", [StringComparison]::Ordinal) -or
        -not $Path.StartsWith("CommunityAI/", [StringComparison]::Ordinal)
    ) {
        return $false
    }
    $normalized = if ($Path.EndsWith("/", [StringComparison]::Ordinal)) {
        $Path.Substring(0, $Path.Length - 1)
    } else {
        $Path
    }
    foreach ($segment in $normalized.Split("/")) {
        if (-not (Test-Gate13SafeWindowsPathSegment -Segment $segment)) {
            return $false
        }
    }
    return $true
}

function Test-Gate13BundledWeightPath {
    param([Parameter(Mandatory = $true)] [string] $Path)
    $extension = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
    return $extension -in @(".safetensors", ".bin", ".pt", ".pth", ".ckpt")
}

function Get-Gate13StreamSha256 {
    param([Parameter(Mandatory = $true)] [System.IO.Stream] $Stream)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($Stream)
        return ([BitConverter]::ToString($hash) -replace "-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Test-Gate13PackageAudit {
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop

    $metadataPath = Join-Path $script:LifecycleAuditRoot "release-metadata.json"
    $provenancePath = Join-Path $script:LifecycleAuditRoot "provenance.json"
    $metricsPath = Join-Path $script:LifecycleAuditRoot "desktop-metrics.json"
    $checksumsPath = Join-Path $script:LifecycleAuditRoot "SHA256SUMS"
    foreach ($path in @(
        $script:LifecycleArchive,
        $metadataPath,
        $provenancePath,
        $metricsPath,
        $checksumsPath,
        $script:LifecycleRunInput,
        $script:LifecycleController
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "qualification input missing"
        }
    }

    $run = Read-Gate13JsonFile -Path $script:LifecycleRunInput
    Assert-Gate13ExactProperties -InputObject $run -Names @(
        "schema_version", "run_id", "source_commit", "package_version",
        "package_sha256", "package_bytes", "model_id", "manifest_digest"
    )
    $runId = Get-Gate13Property $run "run_id"
    $expectedSourceCommit = Get-Gate13Property $run "source_commit"
    $expectedPackageVersion = Get-Gate13Property $run "package_version"
    $expectedPackageDigest = Get-Gate13Property $run "package_sha256"
    $expectedPackageBytes = Get-Gate13Property $run "package_bytes"
    $expectedModelId = Get-Gate13Property $run "model_id"
    $expectedManifestDigest = Get-Gate13Property $run "manifest_digest"
    Assert-Gate13String -Value $runId -Pattern "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
    Assert-Gate13String -Value $expectedSourceCommit -Pattern "^[0-9a-f]{40}$"
    Assert-Gate13String -Value $expectedPackageVersion -Pattern "^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$"
    Assert-Gate13String -Value $expectedPackageDigest -Pattern "^[0-9a-f]{64}$"
    Assert-Gate13String -Value $expectedModelId -Pattern "^[ -~]{1,128}$"
    Assert-Gate13String -Value $expectedManifestDigest -Pattern "^[0-9a-f]{64}$"
    if (
        (Get-Gate13Property $run "schema_version") -ne 1 -or
        (-not ($expectedPackageBytes -is [int]) -and -not ($expectedPackageBytes -is [long])) -or
        [int64]$expectedPackageBytes -lt 1 -or
        -not $script:Profiles.ContainsKey($expectedModelId) -or
        $script:Profiles[$expectedModelId].ManifestDigest -cne $expectedManifestDigest
    ) {
        throw "run input rejected"
    }

    $metadata = Read-Gate13JsonFile -Path $metadataPath
    Assert-Gate13ExactProperties -InputObject $metadata -Names @(
        "schema_version", "product", "package", "release_channel", "warning",
        "unsigned", "publisher_signature", "automatic_updates",
        "supported_platforms", "macos_supported", "credits_enabled",
        "complete_release_qualification", "artifact_root", "artifact_inventory",
        "checksum_manifest", "install_archive_required",
        "install_archive_provenance", "desktop_metrics", "provenance"
    )
    $supported = @((Get-Gate13Property $metadata "supported_platforms"))
    if (
        (Get-Gate13Property $metadata "schema_version") -ne 1 -or
        (Get-Gate13Property $metadata "product") -cne "CommunityAI" -or
        (Get-Gate13Property $metadata "package") -cne "communityai-desktop" -or
        (Get-Gate13Property $metadata "release_channel") -cne "public-alpha" -or
        -not ((Get-Gate13Property $metadata "warning") -is [string]) -or
        -not (Get-Gate13Property $metadata "warning").StartsWith("Unsigned public-alpha", [StringComparison]::Ordinal) -or
        (Get-Gate13Property $metadata "unsigned") -ne $true -or
        (Get-Gate13Property $metadata "publisher_signature") -ne $false -or
        (Get-Gate13Property $metadata "automatic_updates") -ne $false -or
        $supported.Count -ne 2 -or $supported[0] -cne "Windows" -or $supported[1] -cne "Linux" -or
        (Get-Gate13Property $metadata "macos_supported") -ne $false -or
        (Get-Gate13Property $metadata "credits_enabled") -ne $false -or
        (Get-Gate13Property $metadata "complete_release_qualification") -ne $false -or
        (Get-Gate13Property $metadata "artifact_root") -cne "CommunityAI" -or
        (Get-Gate13Property $metadata "artifact_inventory") -cne
            "regular-files-and-relative-internal-file-symlinks-with-file-modes" -or
        (Get-Gate13Property $metadata "checksum_manifest") -cne "SHA256SUMS" -or
        (Get-Gate13Property $metadata "install_archive_required") -ne $true -or
        (Get-Gate13Property $metadata "install_archive_provenance") -cne
            "provenance.json#install_archive" -or
        (Get-Gate13Property $metadata "desktop_metrics") -cne "desktop-metrics.json" -or
        (Get-Gate13Property $metadata "provenance") -cne "provenance.json"
    ) {
        throw "release metadata rejected"
    }

    $provenance = Read-Gate13JsonFile -Path $provenancePath
    Assert-Gate13ExactProperties -InputObject $provenance -Names @(
        "schema_version", "product", "package", "release_channel", "source_commit",
        "source_tree", "build_workflow", "build_platform", "build_python",
        "build_pyinstaller", "artifact_root", "checksum_manifest", "artifacts",
        "install_archive", "desktop_metrics", "catalog_publication_bundle",
        "unsigned", "publisher_signature", "automatic_updates",
        "complete_release_qualification"
    )
    $sourceCommit = Get-Gate13Property $provenance "source_commit"
    $sourceTree = Get-Gate13Property $provenance "source_tree"
    Assert-Gate13String -Value $sourceCommit -Pattern "^[0-9a-f]{40}$"
    Assert-Gate13String -Value $sourceTree -Pattern "^[0-9a-f]{40}$"
    if (
        (Get-Gate13Property $provenance "schema_version") -ne 1 -or
        (Get-Gate13Property $provenance "product") -cne "CommunityAI" -or
        (Get-Gate13Property $provenance "package") -cne "communityai-desktop" -or
        (Get-Gate13Property $provenance "release_channel") -cne "public-alpha" -or
        $sourceCommit -cne $expectedSourceCommit -or
        (Get-Gate13Property $provenance "build_platform") -cne "Windows" -or
        (Get-Gate13Property $provenance "artifact_root") -cne "CommunityAI" -or
        (Get-Gate13Property $provenance "checksum_manifest") -cne "SHA256SUMS" -or
        (Get-Gate13Property $provenance "unsigned") -ne $true -or
        (Get-Gate13Property $provenance "publisher_signature") -ne $false -or
        (Get-Gate13Property $provenance "automatic_updates") -ne $false -or
        (Get-Gate13Property $provenance "complete_release_qualification") -ne $false
    ) {
        throw "provenance rejected"
    }

    $metricsRecord = Get-Gate13Property $provenance "desktop_metrics"
    Assert-Gate13ExactProperties -InputObject $metricsRecord -Names @(
        "schema_version", "path", "sha256", "size_bytes"
    )
    $metricsDigest = Get-Gate13Sha256 -Path $metricsPath
    $metricsBytes = [int64](Get-Item -LiteralPath $metricsPath -Force).Length
    if (
        (Get-Gate13Property $metricsRecord "schema_version") -ne 1 -or
        (Get-Gate13Property $metricsRecord "path") -cne "desktop-metrics.json" -or
        (Get-Gate13Property $metricsRecord "sha256") -cne $metricsDigest -or
        [int64](Get-Gate13Property $metricsRecord "size_bytes") -ne $metricsBytes
    ) {
        throw "desktop metrics provenance rejected"
    }

    $metrics = Read-Gate13JsonFile -Path $metricsPath
    Assert-Gate13ExactProperties -InputObject $metrics -Names @(
        "schema_version", "application", "package", "platform", "python",
        "bundle_bytes", "file_count", "runtime", "acceptance", "ui_smoke_passed",
        "onboarding_ui_smoke_passed", "node_sidecar", "console_window", "signed",
        "catalog_bootstrap_bundled", "catalog_publication_bundle", "release_artifacts"
    )
    $metricArtifacts = @((Get-Gate13Property $provenance "artifacts"))
    [int64]$metricBundleBytes = 0
    [int64]$metricNodeBytes = 0
    $metricNodeFiles = 0
    foreach ($metricArtifact in $metricArtifacts) {
        $metricArtifactPath = Get-Gate13Property $metricArtifact "path"
        $metricArtifactBytes = [int64](Get-Gate13Property $metricArtifact "size_bytes")
        $metricBundleBytes = [int64]($metricBundleBytes + $metricArtifactBytes)
        if (
            $metricArtifactPath -is [string] -and
            $metricArtifactPath.StartsWith("CommunityAI/node/", [StringComparison]::Ordinal)
        ) {
            $metricNodeBytes = [int64]($metricNodeBytes + $metricArtifactBytes)
            $metricNodeFiles += 1
        }
    }
    $metricPublication = Get-Gate13Property $metrics "catalog_publication_bundle"
    $provenancePublication = Get-Gate13Property $provenance "catalog_publication_bundle"
    if (
        (Get-Gate13Property $metrics "schema_version") -ne 1 -or
        (Get-Gate13Property $metrics "application") -cne "CommunityAI" -or
        (Get-Gate13Property $metrics "package") -cne "communityai-desktop" -or
        (Get-Gate13Property $metrics "platform") -cne
            (Get-Gate13Property $provenance "build_platform") -or
        (Get-Gate13Property $metrics "python") -cne
            (Get-Gate13Property $provenance "build_python") -or
        [int64](Get-Gate13Property $metrics "bundle_bytes") -ne $metricBundleBytes -or
        [int](Get-Gate13Property $metrics "file_count") -ne $metricArtifacts.Count -or
        (Get-Gate13Property $metrics "ui_smoke_passed") -ne $true -or
        (Get-Gate13Property $metrics "onboarding_ui_smoke_passed") -ne $true -or
        (Get-Gate13Property $metrics "console_window") -ne $false -or
        (Get-Gate13Property $metrics "signed") -ne $false -or
        (Get-Gate13Property $metrics "catalog_bootstrap_bundled") -ne $true -or
        ($metricPublication | ConvertTo-Json -Depth 32 -Compress) -cne
            ($provenancePublication | ConvertTo-Json -Depth 32 -Compress)
    ) {
        throw "desktop metrics release claims rejected"
    }

    $desktopRuntime = Get-Gate13Property $metrics "runtime"
    Assert-Gate13ExactProperties -InputObject $desktopRuntime -Names @(
        "shell", "framework", "version"
    )
    if (
        (Get-Gate13Property $desktopRuntime "shell") -cne "pyside" -or
        (Get-Gate13Property $desktopRuntime "framework") -cne "PySide6"
    ) {
        throw "desktop metrics runtime rejected"
    }
    Assert-Gate13String -Value (Get-Gate13Property $desktopRuntime "version") -Pattern "^[ -~]{1,128}$"

    $acceptance = Get-Gate13Property $metrics "acceptance"
    Assert-Gate13ExactProperties -InputObject $acceptance -Names @(
        "api_version", "model_count", "worker_actions", "key_lifecycle",
        "contribution_policy", "policy_update", "auto_selection"
    )
    if (
        (Get-Gate13Property $acceptance "api_version") -ne 1 -or
        (Get-Gate13Property $acceptance "model_count") -ne 3 -or
        (Get-Gate13Property $acceptance "worker_actions") -ne 3 -or
        (Get-Gate13Property $acceptance "key_lifecycle") -cne "passed" -or
        (Get-Gate13Property $acceptance "contribution_policy") -cne "passed" -or
        (Get-Gate13Property $acceptance "policy_update") -cne "passed" -or
        (Get-Gate13Property $acceptance "auto_selection") -cne "passed"
    ) {
        throw "desktop metrics acceptance rejected"
    }

    $metricsNode = Get-Gate13Property $metrics "node_sidecar"
    Assert-Gate13ExactProperties -InputObject $metricsNode -Names @(
        "relative_executable", "bundle_bytes", "file_count", "runtime",
        "worker_runtime", "self_test_passed", "worker_self_test_passed",
        "node_entrypoint_smoke_passed", "worker_entrypoint_smoke_passed"
    )
    if (
        (Get-Gate13Property $metricsNode "relative_executable") -cne
            "node/CommunityAI-Node.exe" -or
        [int64](Get-Gate13Property $metricsNode "bundle_bytes") -ne $metricNodeBytes -or
        [int](Get-Gate13Property $metricsNode "file_count") -ne $metricNodeFiles -or
        (Get-Gate13Property $metricsNode "self_test_passed") -ne $true -or
        (Get-Gate13Property $metricsNode "worker_self_test_passed") -ne $true -or
        (Get-Gate13Property $metricsNode "node_entrypoint_smoke_passed") -ne $true -or
        (Get-Gate13Property $metricsNode "worker_entrypoint_smoke_passed") -ne $true
    ) {
        throw "desktop metrics node sidecar rejected"
    }

    $metricsNodeRuntime = Get-Gate13Property $metricsNode "runtime"
    Assert-Gate13ExactProperties -InputObject $metricsNodeRuntime -Names @(
        "schema_version", "application", "drift", "torch", "transformers",
        "hivemind", "fastapi", "uvicorn", "keyring", "p2pd",
        "catalog_bootstrap_schema", "frozen"
    )
    if (
        (Get-Gate13Property $metricsNodeRuntime "schema_version") -ne 1 -or
        (Get-Gate13Property $metricsNodeRuntime "application") -cne "CommunityAI-Node" -or
        (Get-Gate13Property $metricsNodeRuntime "drift") -cne $expectedPackageVersion -or
        (Get-Gate13Property $metricsNodeRuntime "torch") -cne "2.6.0+cu124" -or
        (Get-Gate13Property $metricsNodeRuntime "p2pd") -cne "p2pd.exe" -or
        (Get-Gate13Property $metricsNodeRuntime "catalog_bootstrap_schema") -ne 1 -or
        (Get-Gate13Property $metricsNodeRuntime "frozen") -ne $true
    ) {
        throw "desktop metrics CUDA node runtime rejected"
    }
    foreach ($field in @("drift", "transformers", "hivemind", "fastapi", "uvicorn", "keyring")) {
        Assert-Gate13String -Value (Get-Gate13Property $metricsNodeRuntime $field) -Pattern "^[ -~]{1,128}$"
    }

    $metricsWorker = Get-Gate13Property $metricsNode "worker_runtime"
    Assert-Gate13ExactProperties -InputObject $metricsWorker -Names @(
        "schema_version", "application", "entrypoint", "server_class",
        "model_loading_performed", "network_join_performed", "throughput_mode",
        "training_rpcs_enabled", "process_lifetime_guard_armed", "frozen"
    )
    if (
        (Get-Gate13Property $metricsWorker "schema_version") -ne 1 -or
        (Get-Gate13Property $metricsWorker "application") -cne "CommunityAI-Worker" -or
        (Get-Gate13Property $metricsWorker "entrypoint") -cne "server" -or
        (Get-Gate13Property $metricsWorker "server_class") -cne "Server" -or
        (Get-Gate13Property $metricsWorker "model_loading_performed") -ne $false -or
        (Get-Gate13Property $metricsWorker "network_join_performed") -ne $false -or
        (Get-Gate13Property $metricsWorker "throughput_mode") -cne "dry_run" -or
        (Get-Gate13Property $metricsWorker "training_rpcs_enabled") -ne $false -or
        (Get-Gate13Property $metricsWorker "process_lifetime_guard_armed") -ne $true -or
        (Get-Gate13Property $metricsWorker "frozen") -ne $true
    ) {
        throw "desktop metrics worker runtime rejected"
    }

    $metricsRelease = Get-Gate13Property $metrics "release_artifacts"
    Assert-Gate13ExactProperties -InputObject $metricsRelease -Names @(
        "schema_version", "artifact_count", "artifact_bytes", "checksums_sha256",
        "install_archive", "source_commit", "source_tree", "unsigned",
        "complete_release_qualification"
    )
    if (
        (Get-Gate13Property $metricsRelease "schema_version") -ne 1 -or
        [int](Get-Gate13Property $metricsRelease "artifact_count") -ne $metricArtifacts.Count -or
        [int64](Get-Gate13Property $metricsRelease "artifact_bytes") -ne $metricBundleBytes -or
        (Get-Gate13Property $metricsRelease "checksums_sha256") -cne
            (Get-Gate13Sha256 -Path $checksumsPath) -or
        ((Get-Gate13Property $metricsRelease "install_archive") |
            ConvertTo-Json -Depth 16 -Compress) -cne
            ((Get-Gate13Property $provenance "install_archive") |
                ConvertTo-Json -Depth 16 -Compress) -or
        (Get-Gate13Property $metricsRelease "source_commit") -cne $sourceCommit -or
        (Get-Gate13Property $metricsRelease "source_tree") -cne
            (Get-Gate13Property $provenance "source_tree") -or
        (Get-Gate13Property $metricsRelease "unsigned") -ne $true -or
        (Get-Gate13Property $metricsRelease "complete_release_qualification") -ne $false
    ) {
        throw "desktop metrics release artifact binding rejected"
    }

    $publication = Get-Gate13Property $provenance "catalog_publication_bundle"
    Assert-Gate13ExactProperties -InputObject $publication -Names @(
        "schema_version", "scope", "catalog_id", "catalog_sequence",
        "catalog_digest", "bootstrap_digest", "bundle_index_digest",
        "member_count", "member_digests", "complete_release_qualification"
    )
    $publicationCatalogId = Get-Gate13Property $publication "catalog_id"
    $publicationCatalogDigest = Get-Gate13Property $publication "catalog_digest"
    $publicationBootstrapDigest = Get-Gate13Property $publication "bootstrap_digest"
    $memberDigests = Get-Gate13Property $publication "member_digests"
    $bootstrapMemberDigest = Get-Gate13Property $memberDigests "catalog-bootstrap.json"
    $memberProperties = @($memberDigests.PSObject.Properties)
    if (
        $memberProperties.Count -ne [int](Get-Gate13Property $publication "member_count") -or
        @($memberProperties | Where-Object {
            -not ($_.Name -is [string]) -or
            -not (Test-Gate13SafeArtifactPath ("CommunityAI/" + $_.Name)) -or
            -not ($_.Value -is [string]) -or
            $_.Value -cnotmatch "^sha256:[0-9a-f]{64}$"
        }).Count -ne 0
    ) {
        throw "catalog publication member inventory rejected"
    }
    if (
        (Get-Gate13Property $publication "schema_version") -ne 1 -or
        -not ((Get-Gate13Property $publication "scope") -is [string]) -or
        -not ($publicationCatalogId -is [string]) -or
        $publicationCatalogId -notmatch "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$" -or
        [int64](Get-Gate13Property $publication "catalog_sequence") -lt 1 -or
        -not ($publicationCatalogDigest -is [string]) -or
        $publicationCatalogDigest -cnotmatch "^sha256:[0-9a-f]{64}$" -or
        -not ($publicationBootstrapDigest -is [string]) -or
        $publicationBootstrapDigest -cnotmatch "^sha256:[0-9a-f]{64}$" -or
        -not ((Get-Gate13Property $publication "bundle_index_digest") -is [string]) -or
        (Get-Gate13Property $publication "bundle_index_digest") -cnotmatch "^sha256:[0-9a-f]{64}$" -or
        [int64](Get-Gate13Property $publication "member_count") -lt 1 -or
        (Get-Gate13Property $publication "complete_release_qualification") -ne $false
    ) {
        throw "catalog publication provenance rejected"
    }

    $archiveRecord = Get-Gate13Property $provenance "install_archive"
    Assert-Gate13ExactProperties -InputObject $archiveRecord -Names @(
        "schema_version", "path", "format", "platform", "artifact_root",
        "sha256", "size_bytes", "entry_count", "preserves_executable_modes",
        "preserves_internal_file_symlinks"
    )
    $packageDigest = Get-Gate13Sha256 -Path $script:LifecycleArchive
    $packageBytes = [int64](Get-Item -LiteralPath $script:LifecycleArchive -Force).Length
    if (
        (Get-Gate13Property $archiveRecord "schema_version") -ne 1 -or
        (Get-Gate13Property $archiveRecord "path") -cne "communityai-desktop-windows.zip" -or
        (Get-Gate13Property $archiveRecord "format") -cne "zip" -or
        (Get-Gate13Property $archiveRecord "platform") -cne "Windows" -or
        (Get-Gate13Property $archiveRecord "artifact_root") -cne "CommunityAI" -or
        $packageDigest -cne $expectedPackageDigest -or
        $packageBytes -ne [int64]$expectedPackageBytes -or
        (Get-Gate13Property $archiveRecord "sha256") -cne $packageDigest -or
        [int64](Get-Gate13Property $archiveRecord "size_bytes") -ne $packageBytes -or
        (Get-Gate13Property $archiveRecord "preserves_executable_modes") -ne $false -or
        (Get-Gate13Property $archiveRecord "preserves_internal_file_symlinks") -ne $false
    ) {
        throw "archive provenance rejected"
    }

    $artifactMap = New-Object "System.Collections.Generic.Dictionary[string,object]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($artifact in @((Get-Gate13Property $provenance "artifacts"))) {
        Assert-Gate13ExactProperties -InputObject $artifact -Names @(
            "path", "kind", "mode", "sha256", "size_bytes"
        )
        $artifactPath = Get-Gate13Property $artifact "path"
        $artifactDigest = Get-Gate13Property $artifact "sha256"
        if (
            -not ($artifactPath -is [string]) -or
            -not (Test-Gate13SafeArchivePath $artifactPath) -or
            (Get-Gate13Property $artifact "kind") -cne "file" -or
            -not ((Get-Gate13Property $artifact "mode") -is [int] -or
                  (Get-Gate13Property $artifact "mode") -is [long]) -or
            -not ($artifactDigest -is [string]) -or
            $artifactDigest -notmatch "^[0-9a-f]{64}$" -or
            -not ((Get-Gate13Property $artifact "size_bytes") -is [int] -or
                  (Get-Gate13Property $artifact "size_bytes") -is [long]) -or
            [int64](Get-Gate13Property $artifact "size_bytes") -lt 0 -or
            $artifactMap.ContainsKey($artifactPath)
        ) {
            throw "artifact inventory rejected"
        }
        $artifactMap.Add($artifactPath, $artifact)
    }
    if ($artifactMap.Count -lt 1) {
        throw "artifact inventory rejected"
    }
    $bootstrapArtifactPath = "CommunityAI/_internal/bootstrap/catalog-bootstrap.json"
    if (
        -not $artifactMap.ContainsKey($bootstrapArtifactPath) -or
        -not ($bootstrapMemberDigest -is [string]) -or
        $bootstrapMemberDigest -cnotmatch "^sha256:[0-9a-f]{64}$" -or
        (Get-Gate13Property $artifactMap[$bootstrapArtifactPath] "sha256") -cne
            $bootstrapMemberDigest.Substring(7)
    ) {
        throw "catalog bootstrap artifact binding rejected"
    }

    $checksumMap = New-Object "System.Collections.Generic.Dictionary[string,string]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    $checksumBytes = [System.IO.File]::ReadAllBytes($checksumsPath)
    try {
        if ($checksumBytes.Length -lt 1 -or $checksumBytes.Length -gt $script:LifecycleMaxOutputBytes) {
            throw "checksum inventory rejected"
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $checksumText = $utf8.GetString($checksumBytes)
        foreach ($line in $checksumText -split "\r?\n") {
            if ([string]::IsNullOrEmpty($line)) {
                continue
            }
            if ($line -notmatch "^([0-9a-f]{64})  (CommunityAI/.+)$") {
                throw "checksum inventory rejected"
            }
            $path = $Matches[2]
            if (-not (Test-Gate13SafeArchivePath $path) -or $checksumMap.ContainsKey($path)) {
                throw "checksum inventory rejected"
            }
            $checksumMap.Add($path, $Matches[1])
        }
    }
    finally {
        [Array]::Clear($checksumBytes, 0, $checksumBytes.Length)
        $checksumText = $null
    }
    if ($checksumMap.Count -ne $artifactMap.Count) {
        throw "checksum inventory rejected"
    }
    foreach ($path in $artifactMap.Keys) {
        if (
            -not $checksumMap.ContainsKey($path) -or
            $checksumMap[$path] -cne (Get-Gate13Property $artifactMap[$path] "sha256")
        ) {
            throw "checksum inventory rejected"
        }
    }

    $archive = [System.IO.Compression.ZipFile]::OpenRead($script:LifecycleArchive)
    $entryNames = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    $fileEntries = 0
    try {
        if ($archive.Entries.Count -ne [int](Get-Gate13Property $archiveRecord "entry_count")) {
            throw "archive entry count rejected"
        }
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if (-not (Test-Gate13SafeArchivePath $name) -or -not $entryNames.Add($name)) {
                throw "archive path rejected"
            }
            $isDirectory = $name.EndsWith("/", [StringComparison]::Ordinal)
            if ($isDirectory) {
                if ($entry.Length -ne 0) {
                    throw "archive directory rejected"
                }
                continue
            }
            if (-not $artifactMap.ContainsKey($name)) {
                throw "archive file rejected"
            }
            $artifact = $artifactMap[$name]
            if ([int64]$entry.Length -ne [int64](Get-Gate13Property $artifact "size_bytes")) {
                throw "archive file size rejected"
            }
            $stream = $entry.Open()
            try {
                $digest = Get-Gate13StreamSha256 -Stream $stream
            }
            finally {
                $stream.Dispose()
            }
            if ($digest -cne (Get-Gate13Property $artifact "sha256")) {
                throw "archive file digest rejected"
            }
            $fileEntries += 1
        }
    }
    finally {
        $archive.Dispose()
    }
    if ($fileEntries -ne $artifactMap.Count) {
        throw "archive inventory rejected"
    }

    $weightCount = 0
    [int64]$weightBytes = 0
    foreach ($path in $artifactMap.Keys) {
        if (Test-Gate13BundledWeightPath $path) {
            $weightCount += 1
            $weightBytes = [int64]($weightBytes + [int64](Get-Gate13Property $artifactMap[$path] "size_bytes"))
        }
    }
    if ($weightCount -ne 0 -or $weightBytes -ne 0) {
        throw "package bundles model weights"
    }

    return [pscustomobject]@{
        RunId = $runId
        SourceCommit = $sourceCommit
        ExpectedModelId = $expectedModelId
        ExpectedManifestDigest = $expectedManifestDigest
        PackageDigest = $packageDigest
        PackageBytes = $packageBytes
        ArtifactCount = $artifactMap.Count
        ArtifactMap = $artifactMap
        EntryCount = [int](Get-Gate13Property $archiveRecord "entry_count")
        WeightCount = $weightCount
        WeightBytes = $weightBytes
        PublicationCatalogId = $publicationCatalogId
        PublicationCatalogSequence = [int64](Get-Gate13Property $publication "catalog_sequence")
        PublicationCatalogDigest = $publicationCatalogDigest
        PublicationBootstrapDigest = $publicationBootstrapDigest
        PublicationBootstrapFileDigest = $bootstrapMemberDigest
        MetricsPackageVersion = (Get-Gate13Property $metricsNodeRuntime "drift")
        MetricsTorchVersion = (Get-Gate13Property $metricsNodeRuntime "torch")
    }
}

function Install-Gate13VerifiedPackage {
    param([Parameter(Mandatory = $true)] [object] $Audit)
    if (Test-Path -LiteralPath $script:LifecycleInstallRoot) {
        throw "install root not empty"
    }
    New-Item -ItemType Directory -Path $script:LifecycleInstallRoot -Force -ErrorAction Stop | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory(
        $script:LifecycleArchive,
        $script:LifecycleInstallRoot
    )
    if (
        -not (Test-Path -LiteralPath $script:LifecycleDesktopExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $script:LifecycleNodeExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $script:LifecycleBootstrap -PathType Leaf) -or
        (Get-Gate13FileCount $script:LifecycleProductRoot) -ne $Audit.ArtifactCount
    ) {
        throw "installed package rejected"
    }
    foreach ($path in $Audit.ArtifactMap.Keys) {
        $relative = $path.Substring("CommunityAI/".Length).Replace("/", "\")
        $installed = Join-Path $script:LifecycleProductRoot $relative
        if (
            -not (Test-Path -LiteralPath $installed -PathType Leaf) -or
            [int64](Get-Item -LiteralPath $installed -Force).Length -ne
                [int64](Get-Gate13Property $Audit.ArtifactMap[$path] "size_bytes") -or
            (Get-Gate13Sha256 $installed) -cne
                (Get-Gate13Property $Audit.ArtifactMap[$path] "sha256")
        ) {
            throw "installed artifact rejected"
        }
    }
}


function Test-Gate13PackagedSelfTests {
    foreach ($arguments in @(
        [string[]]@("--check-runtime"),
        [string[]]@("--self-test"),
        [string[]]@("--ui-self-test"),
        [string[]]@("--onboarding-ui-self-test")
    )) {
        $discarded = Invoke-Gate13Contained `
            -Executable $script:LifecycleDesktopExe `
            -Arguments $arguments `
            -WorkingDirectory $script:LifecycleProductRoot `
            -TimeoutSeconds 180
        $discarded = $null
    }

    $nodeText = Invoke-Gate13Contained `
        -Executable $script:LifecycleNodeExe `
        -Arguments ([string[]]@("--self-test")) `
        -WorkingDirectory $script:LifecycleProductRoot `
        -TimeoutSeconds 180
    $node = ConvertFrom-Gate13Json -Payload $nodeText
    $nodeText = $null
    Assert-Gate13ExactProperties -InputObject $node -Names @(
        "schema_version", "application", "drift", "torch", "transformers",
        "hivemind", "fastapi", "uvicorn", "keyring", "p2pd",
        "catalog_bootstrap_schema", "frozen"
    )
    if (
        (Get-Gate13Property $node "schema_version") -ne 1 -or
        (Get-Gate13Property $node "application") -cne "CommunityAI-Node" -or
        (Get-Gate13Property $node "torch") -cne "2.6.0+cu124" -or
        (Get-Gate13Property $node "p2pd") -cne "p2pd.exe" -or
        (Get-Gate13Property $node "catalog_bootstrap_schema") -ne 1 -or
        (Get-Gate13Property $node "frozen") -ne $true
    ) {
        throw "node self-test rejected"
    }
    foreach ($field in @("drift", "torch", "transformers", "hivemind", "fastapi", "uvicorn", "keyring")) {
        $value = Get-Gate13Property $node $field
        if (-not ($value -is [string]) -or $value.Length -lt 1 -or $value.Length -gt 128) {
            throw "node self-test rejected"
        }
    }

    $workerText = Invoke-Gate13Contained `
        -Executable $script:LifecycleNodeExe `
        -Arguments ([string[]]@("server", "--self-test")) `
        -WorkingDirectory $script:LifecycleProductRoot `
        -TimeoutSeconds 180
    $worker = ConvertFrom-Gate13Json -Payload $workerText
    $workerText = $null
    Assert-Gate13ExactProperties -InputObject $worker -Names @(
        "schema_version", "application", "entrypoint", "server_class",
        "model_loading_performed", "network_join_performed", "throughput_mode",
        "training_rpcs_enabled", "process_lifetime_guard_armed", "frozen"
    )
    if (
        (Get-Gate13Property $worker "schema_version") -ne 1 -or
        (Get-Gate13Property $worker "application") -cne "CommunityAI-Worker" -or
        (Get-Gate13Property $worker "entrypoint") -cne "server" -or
        (Get-Gate13Property $worker "server_class") -cne "Server" -or
        (Get-Gate13Property $worker "model_loading_performed") -ne $false -or
        (Get-Gate13Property $worker "network_join_performed") -ne $false -or
        (Get-Gate13Property $worker "throughput_mode") -cne "dry_run" -or
        (Get-Gate13Property $worker "training_rpcs_enabled") -ne $false -or
        (Get-Gate13Property $worker "process_lifetime_guard_armed") -ne $true -or
        (Get-Gate13Property $worker "frozen") -ne $true
    ) {
        throw "worker self-test rejected"
    }
    return [pscustomobject]@{
        PackageVersion = (Get-Gate13Property $node "drift")
        TorchVersion = (Get-Gate13Property $node "torch")
    }
}

function Invoke-Gate13Bootstrap {
    if (
        (Get-Gate13FileCount $script:LifecyclePersistentRoot) -ne 0 -or
        (Get-Gate13DirectoryBytes $script:LifecyclePersistentRoot) -ne 0
    ) {
        throw "persistent root not empty"
    }
    $text = Invoke-Gate13Contained `
        -Executable $script:LifecycleNodeExe `
        -Arguments ([string[]]@(
            "bootstrap",
            $script:LifecycleBootstrap,
            "--data_dir",
            $script:LifecyclePersistentRoot,
            "--node_config",
            $script:LifecycleNodeConfig
        )) `
        -WorkingDirectory $script:LifecycleProductRoot `
        -TimeoutSeconds 300
    $result = ConvertFrom-Gate13Json -Payload $text
    $text = $null
    Assert-Gate13ExactProperties -InputObject $result -Names @(
        "schema_version", "config_path", "catalog_id", "catalog_sequence",
        "catalog_digest", "model_count", "source", "created"
    )
    $catalogId = Get-Gate13Property $result "catalog_id"
    $catalogDigest = Get-Gate13Property $result "catalog_digest"
    if (
        (Get-Gate13Property $result "schema_version") -ne 1 -or
        (Get-Gate13Property $result "config_path") -cne $script:LifecycleNodeConfig -or
        -not ($catalogId -is [string]) -or
        $catalogId -notmatch "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$" -or
        -not ((Get-Gate13Property $result "catalog_sequence") -is [int] -or
              (Get-Gate13Property $result "catalog_sequence") -is [long]) -or
        [int64](Get-Gate13Property $result "catalog_sequence") -lt 1 -or
        -not ($catalogDigest -is [string]) -or
        $catalogDigest -cnotmatch "^sha256:[0-9a-f]{64}$" -or
        -not ((Get-Gate13Property $result "model_count") -is [int] -or
              (Get-Gate13Property $result "model_count") -is [long]) -or
        [int64](Get-Gate13Property $result "model_count") -lt 2 -or
        (Get-Gate13Property $result "created") -ne $true -or
        -not (Test-Path -LiteralPath $script:LifecycleNodeConfig -PathType Leaf)
    ) {
        throw "signed bootstrap rejected"
    }
    return [pscustomobject]@{
        CatalogId = $catalogId
        CatalogSequence = [int64](Get-Gate13Property $result "catalog_sequence")
        CatalogDigest = $catalogDigest
        BootstrapFileDigest = "sha256:" + (Get-Gate13Sha256 $script:LifecycleBootstrap)
    }
}

function Wait-Gate13ProductStatus {
    param([int] $TimeoutSeconds = 300)
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ($null -eq $script:LifecycleProcess -or $script:LifecycleProcess.RootExited) {
            throw "packaged product exited"
        }
        $control = $null
        try {
            $control = Read-Gate13ControlKey
            $status = Invoke-Gate13LoopbackJson `
                -Method "GET" `
                -Path "/control/v1/status" `
                -BearerToken $control
            $profile = Get-Gate13SelectedProfile -Status $status
            return [pscustomobject]@{
                ControlToken = $control
                Status = $status
                Profile = $profile
            }
        }
        catch {
            $control = $null
            Start-Sleep -Milliseconds 250
        }
    }
    throw "packaged product did not become ready"
}

function Get-Gate13SelectedManifestContext {
    param([Parameter(Mandatory = $true)] [object] $Profile)
    $manifestPath = Join-Path (
        Join-Path $script:LifecyclePersistentRoot "manifests"
    ) ($Profile.ManifestDigest + ".json")
    if (
        -not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        (Get-Gate13Sha256 $manifestPath) -cne $Profile.ManifestDigest
    ) {
        throw "selected signed manifest rejected"
    }
    $manifest = Read-Gate13JsonFile -Path $manifestPath
    if (
        (Get-Gate13Property $manifest "schema_version") -ne 1 -or
        (Get-Gate13Property $manifest "name") -cne $Profile.ModelId
    ) {
        throw "selected signed manifest rejected"
    }
    $source = Get-Gate13Property $manifest "source"
    if ((Get-Gate13Property $source "revision") -cne $Profile.RevisionCommit) {
        throw "selected manifest revision rejected"
    }

    $config = Read-Gate13JsonFile -Path $script:LifecycleNodeConfig
    if ((Get-Gate13Property $config "schema_version") -ne 1) {
        throw "node config rejected"
    }
    $matching = @(
        @((Get-Gate13Property $config "models")) | Where-Object {
            $candidate = Get-Gate13Property $_ "manifest"
            [System.IO.Path]::GetFullPath($candidate) -ceq
                [System.IO.Path]::GetFullPath($manifestPath)
        }
    )
    if ($matching.Count -ne 1) {
        throw "selected model config rejected"
    }
    $cacheDir = Get-Gate13Property $matching[0] "cache_dir"
    $expectedCache = Join-Path (
        Join-Path $script:LifecyclePersistentRoot "model-cache"
    ) $Profile.ManifestDigest
    if (
        -not ($cacheDir -is [string]) -or
        [System.IO.Path]::GetFullPath($cacheDir) -cne
            [System.IO.Path]::GetFullPath($expectedCache)
    ) {
        throw "selected cache path rejected"
    }
    return [pscustomobject]@{
        ManifestPath = $manifestPath
        Manifest = $manifest
        ManifestDigest = $Profile.ManifestDigest
        CacheDir = $expectedCache
    }
}

function Test-Gate13NoTransportOverride {
    foreach ($name in @(
        "HF_ENDPOINT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"
    )) {
        if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) {
            throw "transport override present"
        }
    }
}

function Test-Gate13SafeArtifactPath {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (
        [string]::IsNullOrWhiteSpace($Path) -or
        $Path.Contains("\") -or
        $Path.StartsWith("/") -or
        $Path.Contains(":") -or
        $Path.EndsWith("/", [StringComparison]::Ordinal)
    ) {
        return $false
    }
    foreach ($part in $Path.Split("/")) {
        if (-not (Test-Gate13SafeWindowsPathSegment -Segment $part)) {
            return $false
        }
    }
    return $true
}

function Get-Gate13VerifiedArtifactInventory {
    param(
        [Parameter(Mandatory = $true)] [object] $Context,
        [Parameter(Mandatory = $true)] [object[]] $ArtifactRecords
    )
    $root = Join-Path (
        Join-Path (
            Join-Path $Context.CacheDir "manifest-artifacts"
        ) $Context.ManifestDigest
    ) "snapshot"
    $entries = New-Object System.Collections.ArrayList
    $seenPaths = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::Ordinal
    )
    [int64]$total = 0
    foreach ($record in $ArtifactRecords) {
        $relative = Get-Gate13Property $record "path"
        $size = [int64](Get-Gate13Property $record "size_bytes")
        $digest = Get-Gate13Property $record "sha256"
        if (
            -not ($relative -is [string]) -or -not (Test-Gate13SafeArtifactPath $relative) -or
            -not $seenPaths.Add($relative) -or
            $size -lt 1 -or -not ($digest -is [string]) -or
            $digest -notmatch "^[0-9a-f]{64}$"
        ) {
            throw "artifact record rejected"
        }
        $local = Join-Path $root ($relative.Replace("/", "\"))
        if (
            -not (Test-Path -LiteralPath $local -PathType Leaf) -or
            [int64](Get-Item -LiteralPath $local -Force).Length -ne $size -or
            (Get-Gate13Sha256 $local) -cne $digest
        ) {
            throw "artifact cache verification failed"
        }
        [void]$entries.Add([pscustomobject]@{
            RelativePath = $relative
            Size = $size
            Digest = $digest
            LocalPath = $local
        })
        $total = [int64]($total + $size)
    }
    return [pscustomobject]@{
        Entries = @($entries | Sort-Object RelativePath)
        Count = $entries.Count
        Bytes = $total
    }
}

function Assert-Gate13SameArtifactInventory {
    param(
        [Parameter(Mandatory = $true)] [object] $Expected,
        [Parameter(Mandatory = $true)] [object] $Actual
    )
    if (
        $Expected.Count -ne $Actual.Count -or
        [int64]$Expected.Bytes -ne [int64]$Actual.Bytes
    ) {
        throw "verified cache inventory changed"
    }
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        $before = $Expected.Entries[$index]
        $after = $Actual.Entries[$index]
        if (
            $before.RelativePath -cne $after.RelativePath -or
            [int64]$before.Size -ne [int64]$after.Size -or
            $before.Digest -cne $after.Digest
        ) {
            throw "verified cache inventory changed"
        }
    }
}

function Invoke-Gate13VerifiedAcquisition {
    param(
        [Parameter(Mandatory = $true)] [object] $Profile,
        [Parameter(Mandatory = $true)] [object] $Context
    )
    if ($script:LifecycleAcquisitionInvoked) {
        throw "acquisition already invoked"
    }
    if (
        (Get-Gate13FileCount $Context.CacheDir) -ne 0 -or
        (Get-Gate13DirectoryBytes $Context.CacheDir) -ne 0
    ) {
        throw "acquisition cache not empty"
    }
    Test-Gate13NoTransportOverride
    $script:LifecycleAcquisitionInvoked = $true
    $text = Invoke-Gate13Contained `
        -Executable $script:LifecycleNodeExe `
        -Arguments ([string[]]@(
            "edge-acquire",
            $Context.ManifestPath,
            "--cache_dir",
            $Context.CacheDir,
            "--max_resumptions",
            "3",
            "--require_direct_upstream"
        )) `
        -WorkingDirectory $script:LifecycleProductRoot `
        -TimeoutSeconds 3600
    $raw = ConvertFrom-Gate13Json -Payload $text
    $text = $null
    Assert-Gate13ExactProperties -InputObject $raw -Names @(
        "schema_version", "acquired_at_unix", "runtime", "model", "selection",
        "artifacts", "transfer", "storage", "privacy"
    )
    if ((Get-Gate13Property $raw "schema_version") -ne 1) {
        throw "acquisition schema rejected"
    }
    $model = Get-Gate13Property $raw "model"
    $selection = Get-Gate13Property $raw "selection"
    $transfer = Get-Gate13Property $raw "transfer"
    $storage = Get-Gate13Property $raw "storage"
    $privacy = Get-Gate13Property $raw "privacy"
    $artifacts = @((Get-Gate13Property $raw "artifacts"))
    if (
        (Get-Gate13Property $model "id") -cne $Profile.ModelId -or
        (Get-Gate13Property $model "manifest_digest") -cne
            ("sha256:" + $Profile.ManifestDigest) -or
        (Get-Gate13Property $model "revision") -cne $Profile.RevisionCommit -or
        [int64](Get-Gate13Property $selection "artifact_count") -ne
            [int64]$Profile.SelectedCount -or
        [int64](Get-Gate13Property $selection "artifact_bytes") -ne
            [int64]$Profile.SelectedBytes -or
        $artifacts.Count -ne $Profile.SelectedCount
    ) {
        throw "acquisition selection rejected"
    }
    Assert-Gate13ExactProperties -InputObject $transfer -Names @(
        "direct_upstream_transfer", "mirror_used", "source_class_verified",
        "transport_override_present", "elapsed_seconds", "max_resumptions",
        "resumptions", "completed"
    )
    if (
        (Get-Gate13Property $transfer "direct_upstream_transfer") -ne $true -or
        (Get-Gate13Property $transfer "mirror_used") -ne $false -or
        (Get-Gate13Property $transfer "source_class_verified") -ne $true -or
        (Get-Gate13Property $transfer "transport_override_present") -ne $false -or
        (Get-Gate13Property $transfer "max_resumptions") -ne 3 -or
        [int64](Get-Gate13Property $transfer "resumptions") -lt 0 -or
        [int64](Get-Gate13Property $transfer "resumptions") -gt 3 -or
        (Get-Gate13Property $transfer "completed") -ne $true
    ) {
        throw "direct acquisition proof rejected"
    }
    if (
        (Get-Gate13Property $storage "cold_start") -ne $true -or
        [int64](Get-Gate13Property $storage "cache_bytes_before") -ne 0 -or
        [int64](Get-Gate13Property $storage "cache_bytes_after") -ne
            [int64]$Profile.SelectedBytes -or
        [int64](Get-Gate13Property $storage "cache_growth_bytes") -ne
            [int64]$Profile.SelectedBytes -or
        (Get-Gate13Property $storage "verified") -ne $true -or
        (Get-Gate13Property $privacy "credentials_retained") -ne $false -or
        (Get-Gate13Property $privacy "local_paths_retained") -ne $false -or
        (Get-Gate13Property $privacy "response_bodies_retained") -ne $false -or
        (Get-Gate13Property $privacy "urls_retained") -ne $false
    ) {
        throw "acquisition safety proof rejected"
    }

    $selectedPaths = @(
        @((Get-Gate13Property $selection "startup_artifact_paths")) +
        @((Get-Gate13Property $selection "weight_artifact_paths"))
    )
    $selectedSet = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::Ordinal
    )
    foreach ($path in $selectedPaths) {
        if (-not ($path -is [string]) -or -not (Test-Gate13SafeArtifactPath $path) -or
            -not $selectedSet.Add($path)) {
            throw "acquisition selected paths rejected"
        }
    }
    if ($selectedSet.Count -ne $Profile.SelectedCount) {
        throw "acquisition selected paths rejected"
    }

    [int64]$resumeTotal = 0
    $manifestArtifacts = @((Get-Gate13Property $Context.Manifest "artifacts"))
    foreach ($record in $artifacts) {
        Assert-Gate13ExactProperties -InputObject $record -Names @(
            "path", "role", "size_bytes", "sha256", "materialization_attempts",
            "resumptions", "resumed_from_bytes", "elapsed_seconds"
        )
        $relative = Get-Gate13Property $record "path"
        if (-not $selectedSet.Contains($relative)) {
            throw "acquisition selected paths rejected"
        }
        $matches = @($manifestArtifacts | Where-Object {
            (Get-Gate13Property $_ "path") -ceq $relative
        })
        if (
            $matches.Count -ne 1 -or
            [int64](Get-Gate13Property $matches[0] "size") -ne
                [int64](Get-Gate13Property $record "size_bytes") -or
            (Get-Gate13Property $matches[0] "sha256") -cne
                (Get-Gate13Property $record "sha256") -or
            [int64](Get-Gate13Property $record "materialization_attempts") -lt 1 -or
            [int64](Get-Gate13Property $record "materialization_attempts") -gt 4 -or
            [int64](Get-Gate13Property $record "resumptions") -lt 0 -or
            [int64](Get-Gate13Property $record "resumptions") -gt 3
        ) {
            throw "acquired artifact proof rejected"
        }
        $resumeTotal = [int64](
            $resumeTotal + [int64](Get-Gate13Property $record "resumptions")
        )
    }
    if ($resumeTotal -ne [int64](Get-Gate13Property $transfer "resumptions")) {
        throw "acquisition resumption proof rejected"
    }

    $inventory = Get-Gate13VerifiedArtifactInventory `
        -Context $Context `
        -ArtifactRecords $artifacts
    if (
        $inventory.Count -ne $Profile.SelectedCount -or
        [int64]$inventory.Bytes -ne [int64]$Profile.SelectedBytes
    ) {
        throw "acquisition inventory rejected"
    }
    return [pscustomobject]@{
        Artifacts = $artifacts
        Inventory = $inventory
        ResumeCount = $resumeTotal
    }
}


function Get-Gate13SecretMaterialCount {
    $credentialCount = Get-Gate13CredentialCount
    $keyStorePath = Join-Path $script:LifecyclePersistentRoot "api-keys.json"
    if (-not (Test-Path -LiteralPath $keyStorePath -PathType Leaf)) {
        return $credentialCount
    }
    $store = Read-Gate13JsonFile -Path $keyStorePath
    Assert-Gate13ExactProperties -InputObject $store -Names @("schema_version", "keys")
    if ((Get-Gate13Property $store "schema_version") -ne 1) {
        throw "API key store rejected"
    }
    $active = 0
    $seen = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($key in @((Get-Gate13Property $store "keys"))) {
        Assert-Gate13ExactProperties -InputObject $key -Names @(
            "id", "label", "secret_hash", "created_at", "revoked_at"
        )
        $keyId = Get-Gate13Property $key "id"
        $hash = Get-Gate13Property $key "secret_hash"
        if (
            -not ($keyId -is [string]) -or $keyId -notmatch "^key_[0-9a-f]{16}$" -or
            -not $seen.Add($keyId) -or
            -not ($hash -is [string]) -or $hash -notmatch "^[0-9a-f]{64}$"
        ) {
            throw "API key store rejected"
        }
        if ($null -eq (Get-Gate13Property $key "revoked_at")) {
            $active += 1
        }
    }
    if ($active -lt 0 -or $active -gt 64) {
        throw "API key store rejected"
    }
    return $credentialCount + $active
}

function Test-Gate13LocalhostInference {
    $phase = Invoke-Gate13WindowsLocalhostInference
    if (
        $phase.phase -cne "localhost_inference" -or
        $phase.passed -ne $true -or
        $phase.loopback_only -ne $true -or
        $phase.completion_count -ne 1 -or
        $phase.generated_token_count -lt 1 -or
        $phase.response_content_retained -ne $false -or
        $phase.token_identifier_count -ne 0
    ) {
        throw "localhost inference rejected"
    }
    return $phase
}

function Set-Gate13BoundedContribution {
    param(
        [Parameter(Mandatory = $true)] [string] $ControlToken,
        [Parameter(Mandatory = $true)] [object] $Profile
    )
    $snapshot = Invoke-Gate13LoopbackJson `
        -Method "GET" `
        -Path "/control/v1/contribution-policy" `
        -BearerToken $ControlToken
    Assert-Gate13ExactProperties -InputObject $snapshot -Names @(
        "schema_version", "config_revision", "policy"
    )
    $revision = Get-Gate13Property $snapshot "config_revision"
    if (
        (Get-Gate13Property $snapshot "schema_version") -ne 1 -or
        -not ($revision -is [string]) -or
        $revision -notmatch "^sha256:[0-9a-f]{64}$"
    ) {
        throw "contribution policy revision rejected"
    }
    $policy = [ordered]@{
        sharing_enabled = $true
        allowed_models = @($Profile.ModelId)
        preferred_models = @($Profile.ModelId)
        denied_models = @()
        max_disk_space = "32GB"
        max_vram = "20GB"
        max_bandwidth_mbps = 100.0
        max_power_watts = 500.0
        pause_timeout = 120
        schedule = [ordered]@{
            timezone = "UTC"
            windows = @(
                [ordered]@{
                    days = @("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                    start = "00:00"
                    end = "23:59"
                }
            )
        }
    }
    $updated = Invoke-Gate13LoopbackJson `
        -Method "PUT" `
        -Path "/control/v1/contribution-policy" `
        -BearerToken $ControlToken `
        -Body ([ordered]@{
            schema_version = 1
            expected_config_revision = $revision
            policy = $policy
        })
    Assert-Gate13ExactProperties -InputObject $updated -Names @(
        "schema_version", "config_revision", "policy"
    )
    $nextRevision = Get-Gate13Property $updated "config_revision"
    if (
        (Get-Gate13Property $updated "schema_version") -ne 1 -or
        -not ($nextRevision -is [string]) -or
        $nextRevision -notmatch "^sha256:[0-9a-f]{64}$" -or
        $nextRevision -ceq $revision
    ) {
        throw "contribution policy update rejected"
    }
    $returnedPolicy = Get-Gate13Property $updated "policy"
    Assert-Gate13ExactProperties -InputObject $returnedPolicy -Names @(
        "sharing_enabled", "allowed_models", "preferred_models", "denied_models",
        "max_disk_space", "max_vram", "max_bandwidth_mbps",
        "max_power_watts", "pause_timeout", "schedule"
    )
    if (
        (Get-Gate13Property $returnedPolicy "sharing_enabled") -ne $true -or
        @((Get-Gate13Property $returnedPolicy "allowed_models")).Count -ne 1 -or
        @((Get-Gate13Property $returnedPolicy "allowed_models"))[0] -cne $Profile.ModelId -or
        @((Get-Gate13Property $returnedPolicy "preferred_models")).Count -ne 1 -or
        @((Get-Gate13Property $returnedPolicy "preferred_models"))[0] -cne $Profile.ModelId -or
        @((Get-Gate13Property $returnedPolicy "denied_models")).Count -ne 0 -or
        (Get-Gate13Property $returnedPolicy "max_disk_space") -cne "32GB" -or
        (Get-Gate13Property $returnedPolicy "max_vram") -cne "20GB" -or
        [double](Get-Gate13Property $returnedPolicy "max_bandwidth_mbps") -ne 100.0 -or
        [double](Get-Gate13Property $returnedPolicy "max_power_watts") -ne 500.0 -or
        [int64](Get-Gate13Property $returnedPolicy "pause_timeout") -ne 120
    ) {
        throw "contribution policy snapshot rejected"
    }
}

function Get-Gate13ExactWorkerSnapshot {
    param([Parameter(Mandatory = $true)] [string] $ControlToken)
    $response = Invoke-Gate13LoopbackJson `
        -Method "GET" `
        -Path "/control/v1/workers" `
        -BearerToken $ControlToken
    $workers = @((Get-Gate13Property $response "workers"))
    $automatic = @($workers | Where-Object {
        (Get-Gate13Property $_ "id") -ceq "automatic"
    })
    if ($automatic.Count -ne 1) {
        throw "exact automatic worker snapshot rejected"
    }
    return [pscustomobject]@{
        Workers = @($workers)
        Automatic = $automatic[0]
    }
}

function Wait-Gate13ContributionWorker {
    param(
        [Parameter(Mandatory = $true)] [string] $ControlToken,
        [Parameter(Mandatory = $true)] [object] $Profile,
        [int] $TimeoutSeconds = 1800
    )
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $status = Invoke-Gate13LoopbackJson `
            -Method "GET" `
            -Path "/control/v1/status" `
            -BearerToken $ControlToken
        $selected = Get-Gate13SelectedProfile -Status $status
        if (
            $selected.ModelId -cne $Profile.ModelId -or
            $selected.ManifestDigest -cne $Profile.ManifestDigest
        ) {
            throw "model selection changed"
        }
        $contribution = Get-Gate13Property $status "contribution"
        if (
            (Get-Gate13Property $contribution "schema_version") -ne 3 -or
            (Get-Gate13Property $contribution "configured") -ne $true -or
            (Get-Gate13Property $contribution "editable") -ne $true
        ) {
            throw "contribution status rejected"
        }
        $active = @(
            @((Get-Gate13Property $contribution "workers")) | Where-Object {
                (Get-Gate13Property $_ "desired_running") -eq $true -and
                (Get-Gate13Property $_ "state") -in @("starting", "running")
            }
        )
        if ($active.Count -gt 1) {
            throw "multiple active contribution workers"
        }
        if ($active.Count -eq 1 -and
            (Get-Gate13Property $active[0] "state") -ceq "running") {
            $worker = $active[0]
            if (
                (Get-Gate13Property $worker "id") -cne "automatic" -or
                (Get-Gate13Property $worker "model") -cne $Profile.ModelId
            ) {
                throw "active contribution worker rejected"
            }
            $placement = Get-Gate13Property $worker "placement"
            $blockIndices = Get-Gate13Property $placement "block_indices"
            if (
                (Get-Gate13Property $placement "automatic") -ne $true -or
                -not ($blockIndices -is [string]) -or
                $blockIndices -notmatch "^([0-9]{1,3}):([0-9]{1,3})$"
            ) {
                throw "automatic placement rejected"
            }
            $blockStart = [int]$Matches[1]
            $blockEnd = [int]$Matches[2]
            if ($blockEnd -le $blockStart -or $blockEnd -gt 512) {
                throw "automatic placement rejected"
            }
            $policyGate = Get-Gate13Property $worker "policy"
            $scheduleGate = Get-Gate13Property $worker "schedule"
            $resources = Get-Gate13Property $worker "resources"
            $limits = Get-Gate13Property $resources "limits"
            if (
                (Get-Gate13Property $policyGate "admitted") -ne $true -or
                (Get-Gate13Property $policyGate "preferred") -ne $true -or
                (Get-Gate13Property $scheduleGate "admitted") -ne $true -or
                (Get-Gate13Property $scheduleGate "suspended") -ne $false -or
                (Get-Gate13Property $resources "admitted") -ne $true -or
                (Get-Gate13Property $resources "suspended") -ne $false -or
                $null -eq (Get-Gate13Property $limits "disk_bytes") -or
                $null -eq (Get-Gate13Property $limits "vram_bytes") -or
                $null -eq (Get-Gate13Property $limits "bandwidth_mbps") -or
                $null -eq (Get-Gate13Property $limits "power_watts")
            ) {
                throw "bounded contribution limits rejected"
            }
            $exactSnapshot = Get-Gate13ExactWorkerSnapshot -ControlToken $ControlToken
            $exactWorker = $exactSnapshot.Automatic
            $exactActive = @($exactSnapshot.Workers | Where-Object {
                (Get-Gate13Property $_ "desired_running") -eq $true -or
                (Get-Gate13Property $_ "state") -in @("starting", "running", "stopping")
            })
            $workerPid = Get-Gate13Property $exactWorker "pid"
            if (
                $exactActive.Count -ne 1 -or
                (Get-Gate13Property $exactWorker "model") -cne $Profile.ModelId -or
                (Get-Gate13Property $exactWorker "state") -cne "running" -or
                (Get-Gate13Property $exactWorker "desired_running") -ne $true -or
                (Get-Gate13Property $exactWorker "automatic") -ne $true -or
                (-not ($workerPid -is [int]) -and -not ($workerPid -is [long])) -or
                [int64]$workerPid -lt 1 -or [int64]$workerPid -gt [int]::MaxValue -or
                $null -eq (Get-Process -Id ([int]$workerPid) -ErrorAction SilentlyContinue)
            ) {
                throw "exact active contribution worker rejected"
            }
            return [pscustomobject]@{
                BlockStart = $blockStart
                BlockEnd = $blockEnd
                WorkerPid = [int]$workerPid
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "contribution worker did not become active"
}

function Wait-Gate13ContributionPaused {
    param(
        [Parameter(Mandatory = $true)] [string] $ControlToken,
        [Parameter(Mandatory = $true)] [int] $BaselineProcessCount,
        [Parameter(Mandatory = $true)] [int] $WorkerPid,
        [int] $TimeoutSeconds = 300
    )
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $status = Invoke-Gate13LoopbackJson `
            -Method "GET" `
            -Path "/control/v1/status" `
            -BearerToken $ControlToken
        $contribution = Get-Gate13Property $status "contribution"
        $workers = @((Get-Gate13Property $contribution "workers"))
        $active = @($workers | Where-Object {
            (Get-Gate13Property $_ "desired_running") -eq $true -or
            (Get-Gate13Property $_ "state") -in @("starting", "running", "stopping")
        })
        $automatic = @($workers | Where-Object {
            (Get-Gate13Property $_ "id") -ceq "automatic"
        })

        $exactSnapshot = Get-Gate13ExactWorkerSnapshot -ControlToken $ControlToken
        $exactWorker = $exactSnapshot.Automatic
        $exactActive = @($exactSnapshot.Workers | Where-Object {
            (Get-Gate13Property $_ "desired_running") -eq $true -or
            (Get-Gate13Property $_ "state") -in @("starting", "running", "stopping")
        })
        $workerProcessAbsent = $null -eq (
            Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue
        )
        if (
            $active.Count -eq 0 -and
            $automatic.Count -eq 1 -and
            (Get-Gate13Property $automatic[0] "state") -ceq "paused" -and
            (Get-Gate13Property $automatic[0] "desired_running") -eq $false -and
            $exactActive.Count -eq 0 -and
            (Get-Gate13Property $exactWorker "state") -ceq "paused" -and
            (Get-Gate13Property $exactWorker "desired_running") -eq $false -and
            (Get-Gate13Property $exactWorker "operator_paused") -eq $true -and
            $null -eq (Get-Gate13Property $exactWorker "pid") -and
            $workerProcessAbsent -and
            $null -ne $script:LifecycleProcess -and
            $script:LifecycleProcess.ActiveProcessCount -eq $BaselineProcessCount
        ) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "contribution worker did not pause cleanly"
}


function Get-Gate13ProductProcessCount {
    return @(
        Get-Process -Name "CommunityAI", "CommunityAI-Node" -ErrorAction SilentlyContinue
    ).Count
}

function Invoke-Gate13Controller {
    param([Parameter(Mandatory = $true)] [object] $Document)
    $python = Get-Command "python.exe" -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    if ($null -eq $python -or -not [System.IO.Path]::IsPathRooted($python.Source)) {
        throw "qualification controller runtime unavailable"
    }
    $payload = ConvertTo-Json -InputObject $Document -Compress -Depth 16
    if ([System.Text.Encoding]::UTF8.GetByteCount($payload) -gt $script:LifecycleMaxOutputBytes) {
        throw "lifecycle document exceeded bound"
    }
    Initialize-Gate13NativeHost
    $result = [Gate13.NativeHost]::RunWithInput(
        $python.Source,
        [string[]]@($script:LifecycleController),
        $PSScriptRoot,
        180000,
        $script:LifecycleMaxOutputBytes,
        $payload
    )
    $payload = $null
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Output)) {
        throw "lifecycle controller rejected run"
    }
    $canonical = $result.Output.Trim()
    $summary = ConvertFrom-Gate13Json -Payload $canonical
    if (
        (Get-Gate13Property $summary "schema_version") -ne 1 -or
        (Get-Gate13Property $summary "scope") -cne "gate13-packaged-lifecycle" -or
        (Get-Gate13Property $summary "result") -cne "passed" -or
        (Get-Gate13Property $summary "platform") -cne "windows"
    ) {
        throw "lifecycle controller output rejected"
    }
    return $canonical
}

function Invoke-Gate13WindowsPackagedLifecycle {
    Assert-Gate13NoTranscript
    Initialize-Gate13CredentialInterop
    Initialize-Gate13NativeHost

    $state = [pscustomobject]@{
        Audit = $null
        SelfTests = $null
        Bootstrap = $null
        ProductStatus = $null
        Profile = $null
        Context = $null
        Acquisition = $null
        BaselineProcesses = 0
        WorkerPid = 0
        SecretCount = 0
    }
    $phases = New-Object System.Collections.ArrayList

    [void]$phases.Add((Measure-Gate13Phase -Name "package_verification" -Action {
        $state.Audit = Test-Gate13PackageAudit
        return [ordered]@{
            package_sha256 = $state.Audit.PackageDigest
            package_bytes = [int64]$state.Audit.PackageBytes
            checksum_inventory_verified = $true
            provenance_verified = $true
            release_metadata_verified = $true
            unsigned_alpha_acknowledged = $true
            publisher_signature_present = $false
            authenticated_update_present = $false
            bundled_weight_file_count = [int]$state.Audit.WeightCount
            bundled_weight_bytes = [int64]$state.Audit.WeightBytes
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "clean_install" -Action {
        $preexistingProduct = Get-Gate13FileCount $script:LifecycleInstallRoot
        $preexistingPersistent = Get-Gate13FileCount $script:LifecyclePersistentRoot
        $preexistingSecrets = Get-Gate13SecretMaterialCount
        $preexistingProcesses = Get-Gate13ProductProcessCount
        if (
            $preexistingProduct -ne 0 -or
            $preexistingPersistent -ne 0 -or
            $preexistingSecrets -ne 0 -or
            $preexistingProcesses -ne 0 -or
            (Test-Path -LiteralPath $script:LifecycleWorkRoot)
        ) {
            throw "clean host precondition failed"
        }
        $script:LifecycleOwnWorkRoot = $true
        $script:LifecycleOwnPersistentRoot = $true
        New-Item -ItemType Directory -Path $script:LifecycleWorkRoot -ErrorAction Stop |
            Out-Null
        Install-Gate13VerifiedPackage -Audit $state.Audit
        return [ordered]@{
            clean_host = $true
            preexisting_product_file_count = $preexistingProduct
            preexisting_persistent_file_count = $preexistingPersistent
            preexisting_secret_material_count = $preexistingSecrets
            installed_product_file_count = Get-Gate13FileCount $script:LifecycleProductRoot
            source_checkout_present = $false
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "packaged_self_tests" -Action {
        $state.SelfTests = Test-Gate13PackagedSelfTests
        if (
            $state.SelfTests.PackageVersion -cne $state.Audit.MetricsPackageVersion -or
            $state.SelfTests.TorchVersion -cne $state.Audit.MetricsTorchVersion
        ) {
            throw "live packaged runtime does not match provenance-bound metrics"
        }
        if ((Get-Gate13CredentialCount) -ne 0) {
            throw "packaged self-test retained credential"
        }
        return [ordered]@{
            desktop_self_test_passed = $true
            node_self_test_passed = $true
            worker_self_test_passed = $true
            bootstrap_payload_present = (
                Test-Path -LiteralPath $script:LifecycleBootstrap -PathType Leaf
            )
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "signed_bootstrap" -Action {
        $state.Bootstrap = Invoke-Gate13Bootstrap
        if (
            $state.Bootstrap.CatalogId -cne $state.Audit.PublicationCatalogId -or
            [int64]$state.Bootstrap.CatalogSequence -ne
                [int64]$state.Audit.PublicationCatalogSequence -or
            $state.Bootstrap.CatalogDigest -cne
                $state.Audit.PublicationCatalogDigest -or
            $state.Bootstrap.BootstrapFileDigest -cne
                $state.Audit.PublicationBootstrapFileDigest
        ) {
            throw "installed bootstrap did not match release provenance"
        }
        Start-Gate13Product
        $state.ProductStatus = Wait-Gate13ProductStatus -TimeoutSeconds 300
        $state.Profile = $state.ProductStatus.Profile
        if (
            $state.Profile.ModelId -cne $state.Audit.ExpectedModelId -or
            $state.Profile.ManifestDigest -cne $state.Audit.ExpectedManifestDigest
        ) {
            throw "operator-bound selected model identity rejected"
        }
        $state.Context = Get-Gate13SelectedManifestContext -Profile $state.Profile
        return [ordered]@{
            catalog_id = $state.Bootstrap.CatalogId
            catalog_sequence = [int64]$state.Bootstrap.CatalogSequence
            catalog_digest = $state.Bootstrap.CatalogDigest
            catalog_signature_verified = $true
            bootstrap_digest = $state.Audit.PublicationBootstrapDigest
            bootstrap_verified = $true
            manifest_digest = $state.Profile.ManifestDigest
            model_id = $state.Profile.ModelId
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "selected_bytes" -Action {
        if (
            (Get-Gate13FileCount $state.Context.CacheDir) -ne 0 -or
            (Get-Gate13DirectoryBytes $state.Context.CacheDir) -ne 0
        ) {
            throw "selected cache was not empty"
        }
        return [ordered]@{
            manifest_digest = $state.Profile.ManifestDigest
            model_id = $state.Profile.ModelId
            selected_artifact_count = [int]$state.Profile.SelectedCount
            selected_artifact_bytes = [int64]$state.Profile.SelectedBytes
            cache_verified_artifact_bytes_before = [int64]0
            transfer_started = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "verified_acquisition" -Action {
        Stop-Gate13Product
        $state.ProductStatus = $null
        $state.Acquisition = Invoke-Gate13VerifiedAcquisition `
            -Profile $state.Profile `
            -Context $state.Context
        return [ordered]@{
            manifest_digest = $state.Profile.ManifestDigest
            model_id = $state.Profile.ModelId
            revision_commit = $state.Profile.RevisionCommit
            selected_artifact_count = [int]$state.Profile.SelectedCount
            selected_artifact_bytes = [int64]$state.Profile.SelectedBytes
            acquired_artifact_count = [int]$state.Acquisition.Inventory.Count
            acquired_artifact_bytes = [int64]$state.Acquisition.Inventory.Bytes
            artifact_digest_verification_count = [int]$state.Acquisition.Inventory.Count
            resume_count = [int]$state.Acquisition.ResumeCount
            direct_upstream_transfer = $true
            mirror_used = $false
            cache_verified_artifact_bytes_after = [int64]$state.Acquisition.Inventory.Bytes
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "localhost_inference" -Action {
        Start-Gate13Product
        $state.ProductStatus = Wait-Gate13ProductStatus -TimeoutSeconds 300
        $inference = Test-Gate13LocalhostInference
        return [ordered]@{
            loopback_only = $true
            manifest_digest = $state.Profile.ManifestDigest
            model_id = $state.Profile.ModelId
            completion_count = [int]$inference.completion_count
            generated_token_count = [int64]$inference.generated_token_count
            response_content_retained = $false
            token_identifier_count = 0
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "bounded_contribution" -Action {
        Assert-Gate13NoTranscript
        $control = Read-Gate13ControlKey
        try {
            Set-Gate13BoundedContribution -ControlToken $control -Profile $state.Profile
            $state.BaselineProcesses = $script:LifecycleProcess.ActiveProcessCount
            $discarded = Invoke-Gate13LoopbackJson `
                -Method "POST" `
                -Path "/control/v1/workers/automatic/start" `
                -BearerToken $control
            $discarded = $null
            $placement = Wait-Gate13ContributionWorker `
                -ControlToken $control `
                -Profile $state.Profile `
                -TimeoutSeconds 1800
            $state.WorkerPid = [int]$placement.WorkerPid
            return [ordered]@{
                opt_in = $true
                automatic_placement = $true
                manifest_digest = $state.Profile.ManifestDigest
                model_id = $state.Profile.ModelId
                worker_count = 1
                block_start = [int]$placement.BlockStart
                block_end = [int]$placement.BlockEnd
                block_count = [int]($placement.BlockEnd - $placement.BlockStart)
                resource_limit_count = 5
                limits_enforced = $true
                accepted_request_count = 0
                source_imports_used = $false
            }
        }
        finally {
            $control = $null
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "contribution_pause" -Action {
        Assert-Gate13NoTranscript
        $control = Read-Gate13ControlKey
        $pauseTimer = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $prePauseSnapshot = Get-Gate13ExactWorkerSnapshot -ControlToken $control
            $prePauseWorker = $prePauseSnapshot.Automatic
            if (
                (Get-Gate13Property $prePauseWorker "state") -cne "running" -or
                (Get-Gate13Property $prePauseWorker "desired_running") -ne $true -or
                [int64](Get-Gate13Property $prePauseWorker "pid") -ne [int64]$state.WorkerPid
            ) {
                throw "automatic worker identity changed before pause"
            }
            $prePauseSnapshot = $null
            $prePauseWorker = $null
            $discarded = Invoke-Gate13LoopbackJson `
                -Method "POST" `
                -Path "/control/v1/workers/automatic/pause" `
                -BearerToken $control
            $discarded = $null
            Wait-Gate13ContributionPaused `
                -ControlToken $control `
                -BaselineProcessCount $state.BaselineProcesses `
                -WorkerPid $state.WorkerPid `
                -TimeoutSeconds 300
            $pauseTimer.Stop()
            return [ordered]@{
                pause_requested = $true
                pause_completed = $true
                pause_seconds = [Math]::Round($pauseTimer.Elapsed.TotalSeconds, 6)
                worker_count_after = 0
                process_count_after = 0
            }
        }
        finally {
            $control = $null
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "restart_cache_reuse" -Action {
        Test-Gate13NoTransportOverride
        $before = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        Stop-Gate13Product
        Start-Gate13Product
        $state.ProductStatus = Wait-Gate13ProductStatus -TimeoutSeconds 300
        $inference = Test-Gate13LocalhostInference
        $after = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        Assert-Gate13SameArtifactInventory -Expected $before -Actual $after
        if (-not $script:LifecycleAcquisitionInvoked) {
            throw "acquisition proof lost"
        }
        return [ordered]@{
            restart_completed = $true
            manifest_digest = $state.Profile.ManifestDigest
            verified_artifact_bytes_before = [int64]$before.Bytes
            verified_artifact_bytes_after = [int64]$after.Bytes
            transferred_artifact_bytes = [int64]0
            cache_reused = $true
            localhost_inference_passed = ($inference.passed -eq $true)
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "manual_replacement" -Action {
        Test-Gate13NoTransportOverride
        $before = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        $secretBefore = Get-Gate13SecretMaterialCount
        Stop-Gate13Product
        Remove-Gate13ExactTree -Path $script:LifecycleInstallRoot
        Install-Gate13VerifiedPackage -Audit $state.Audit
        $afterInstall = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        Assert-Gate13SameArtifactInventory -Expected $before -Actual $afterInstall
        $secretAfter = Get-Gate13SecretMaterialCount
        if ($secretBefore -lt 1 -or $secretAfter -ne $secretBefore) {
            throw "manual replacement secret state changed"
        }
        Start-Gate13Product
        $state.ProductStatus = Wait-Gate13ProductStatus -TimeoutSeconds 300
        $inference = Test-Gate13LocalhostInference
        return [ordered]@{
            replacement_kind = "reinstall"
            previous_package_sha256 = $state.Audit.PackageDigest
            replacement_package_sha256 = $state.Audit.PackageDigest
            replacement_package_bytes = [int64]$state.Audit.PackageBytes
            checksum_inventory_verified = $true
            provenance_verified = $true
            manual_operation = $true
            automatic_update_used = $false
            publisher_signature_claimed = $false
            verified_artifact_bytes_before = [int64]$before.Bytes
            verified_artifact_bytes_after = [int64]$afterInstall.Bytes
            secret_material_count_before = [int]$secretBefore
            secret_material_count_after = [int]$secretAfter
            localhost_inference_passed = ($inference.passed -eq $true)
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "recovery" -Action {
        Stop-Gate13Product
        Start-Gate13Product
        Start-Sleep -Milliseconds 500
        Stop-Gate13ProductForFault
        Start-Gate13Product
        $state.ProductStatus = Wait-Gate13ProductStatus -TimeoutSeconds 300
        $inference = Test-Gate13LocalhostInference
        $after = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        Assert-Gate13SameArtifactInventory `
            -Expected $state.Acquisition.Inventory `
            -Actual $after
        return [ordered]@{
            recovery_action_count = 2
            fault_observed = $true
            recovery_completed = $true
            verified_artifact_bytes_after = [int64]$after.Bytes
            localhost_inference_passed = ($inference.passed -eq $true)
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "uninstall_retain" -Action {
        $secretBefore = Get-Gate13SecretMaterialCount
        Stop-Gate13Product
        Remove-Gate13ExactTree -Path $script:LifecycleInstallRoot
        $after = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        $secretAfter = Get-Gate13SecretMaterialCount
        if (
            (Get-Gate13FileCount $script:LifecycleInstallRoot) -ne 0 -or
            (Get-Gate13ProductProcessCount) -ne 0 -or
            $secretBefore -lt 1 -or $secretAfter -ne $secretBefore
        ) {
            throw "retain uninstall rejected"
        }
        $state.SecretCount = $secretAfter
        return [ordered]@{
            uninstall_completed = $true
            retain_choice_explicit = $true
            installed_product_file_count_after = 0
            process_count_after = 0
            persistent_file_count_after = Get-Gate13FileCount $script:LifecyclePersistentRoot
            verified_artifact_bytes_after = [int64]$after.Bytes
            secret_material_count_before = [int]$secretBefore
            secret_material_count_after = [int]$secretAfter
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "retained_data_reinstall" -Action {
        Test-Gate13NoTransportOverride
        $before = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        $secretBefore = Get-Gate13SecretMaterialCount
        Install-Gate13VerifiedPackage -Audit $state.Audit
        Start-Gate13Product
        $state.ProductStatus = Wait-Gate13ProductStatus -TimeoutSeconds 300
        $inference = Test-Gate13LocalhostInference
        $after = Get-Gate13VerifiedArtifactInventory `
            -Context $state.Context `
            -ArtifactRecords $state.Acquisition.Artifacts
        Assert-Gate13SameArtifactInventory -Expected $before -Actual $after
        $secretAfter = Get-Gate13SecretMaterialCount
        if (
            $secretBefore -ne $state.SecretCount -or
            $secretAfter -ne $secretBefore
        ) {
            throw "retained secret material changed"
        }
        return [ordered]@{
            install_completed = $true
            verified_artifact_bytes_before = [int64]$before.Bytes
            verified_artifact_bytes_after = [int64]$after.Bytes
            transferred_artifact_bytes = [int64]0
            secret_material_count_before = [int]$secretBefore
            secret_material_count_after = [int]$secretAfter
            cache_reused = $true
            secret_material_reused = $true
            localhost_inference_passed = ($inference.passed -eq $true)
            source_imports_used = $false
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "uninstall_delete" -Action {
        Stop-Gate13Product
        $discarded = Invoke-Gate13Contained `
            -Executable $script:LifecycleDesktopExe `
            -Arguments ([string[]]@("--delete-control-key")) `
            -WorkingDirectory $script:LifecycleProductRoot `
            -TimeoutSeconds 180
        $discarded = $null
        Remove-Gate13ExactTree -Path $script:LifecyclePersistentRoot
        Remove-Gate13ExactTree -Path $script:LifecycleInstallRoot
        if (
            (Get-Gate13FileCount $script:LifecycleInstallRoot) -ne 0 -or
            (Get-Gate13FileCount $script:LifecyclePersistentRoot) -ne 0 -or
            (Get-Gate13DirectoryBytes $script:LifecyclePersistentRoot) -ne 0 -or
            (Get-Gate13SecretMaterialCount) -ne 0 -or
            (Get-Gate13ProductProcessCount) -ne 0
        ) {
            throw "delete uninstall rejected"
        }
        return [ordered]@{
            uninstall_completed = $true
            delete_choice_explicit = $true
            installed_product_file_count_after = 0
            process_count_after = 0
            persistent_file_count_after = 0
            persistent_data_bytes_after = [int64]0
            secret_material_count_after = 0
        }
    }))

    [void]$phases.Add((Measure-Gate13Phase -Name "process_cleanup" -Action {
        Remove-Gate13ExactTree -Path $script:LifecycleWorkRoot
        if (
            (Get-Gate13ProductProcessCount) -ne 0 -or
            (Get-Gate13SecretMaterialCount) -ne 0 -or
            (Test-Path -LiteralPath $script:LifecycleWorkRoot) -or
            (Test-Path -LiteralPath $script:LifecyclePersistentRoot)
        ) {
            throw "final cleanup rejected"
        }
        return [ordered]@{
            cleanup_complete = $true
            product_file_count = 0
            persistent_file_count = 0
            persistent_data_bytes = [int64]0
            secret_material_count = 0
            process_count = 0
            temporary_file_count = 0
        }
    }))

    $document = [ordered]@{
        schema_version = 1
        run_id = $state.Audit.RunId
        platform = "windows"
        source_commit = $state.Audit.SourceCommit
        package_version = $state.SelfTests.PackageVersion
        package_sha256 = $state.Audit.PackageDigest
        package_bytes = [int64]$state.Audit.PackageBytes
        model_id = $state.Profile.ModelId
        manifest_digest = $state.Profile.ManifestDigest
        phases = @($phases)
    }
    return Invoke-Gate13Controller -Document $document
}

function Invoke-Gate13FailureCleanup {
    try {
        Force-Gate13ProductCleanup
    }
    catch {
    }
    try {
        if (
            $script:LifecycleOwnWorkRoot -and
            (Test-Path -LiteralPath $script:LifecycleDesktopExe -PathType Leaf) -and
            (Get-Gate13ProductProcessCount) -eq 0
        ) {
            $discarded = Invoke-Gate13Contained `
                -Executable $script:LifecycleDesktopExe `
                -Arguments ([string[]]@("--delete-control-key")) `
                -WorkingDirectory $script:LifecycleProductRoot `
                -TimeoutSeconds 180
            $discarded = $null
        }
    }
    catch {
    }
    if ($script:LifecycleOwnPersistentRoot) {
        try {
            Remove-Gate13ExactTree -Path $script:LifecyclePersistentRoot
        }
        catch {
        }
    }
    if ($script:LifecycleOwnWorkRoot) {
        try {
            Remove-Gate13ExactTree -Path $script:LifecycleInstallRoot
        }
        catch {
        }
        try {
            Remove-Gate13ExactTree -Path $script:LifecycleWorkRoot
        }
        catch {
        }
    }
}

function Start-Gate13WindowsPackagedLifecycle {
    $canonical = $null
    try {
        $canonical = Invoke-Gate13WindowsPackagedLifecycle
        [Console]::Out.WriteLine($canonical)
        return 0
    }
    catch {
        Invoke-Gate13FailureCleanup
        [Console]::Out.WriteLine(
            '{"failure_code":"windows_packaged_lifecycle_failed","result":"failed","schema_version":1}'
        )
        return 2
    }
    finally {
        $canonical = $null
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
        [GC]::Collect()
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    if ($args.Count -ne 0) {
        [Console]::Out.WriteLine(
            '{"failure_code":"windows_packaged_lifecycle_failed","result":"failed","schema_version":1}'
        )
        exit 2
    }
    $code = Start-Gate13WindowsPackagedLifecycle
    exit $code
}
