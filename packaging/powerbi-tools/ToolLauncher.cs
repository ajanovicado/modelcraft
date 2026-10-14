// Build with the standard Windows .NET Framework compiler. Every launch validates
// the manifest whose SHA-256 was bound into this executable at build time.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

internal static class ToolLauncher
{
    private const string ManifestHash = "__PAYLOAD_MANIFEST_SHA256__";

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits
    {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimits
    {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits limits, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        public uint Size;
        public string Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
        public ushort Show, ReservedSize;
        public IntPtr ReservedData, Input, Output, Error;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInfo
    {
        public IntPtr Process, Thread;
        public uint ProcessId, ThreadId;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string application, StringBuilder commandLine,
        IntPtr processSecurity, IntPtr threadSecurity, bool inheritHandles, uint flags,
        IntPtr environment, string directory, ref StartupInfo startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetStdHandle(int handle);

    private static int Run(ProcessStartInfo start)
    {
        // Closing this job kills the child process tree even if Modelcraft times
        // out or force-terminates the launcher. No orphan Node/Python workers.
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new IOException("Could not create the tool process job.");
        try
        {
            var limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
                throw new IOException("Could not configure the tool process job.");
            var startup = new StartupInfo();
            startup.Size = (uint)Marshal.SizeOf(typeof(StartupInfo));
            startup.Flags = 0x100; // STARTF_USESTDHANDLES: preserve captured stdout/stderr
            startup.Input = GetStdHandle(-10);
            startup.Output = GetStdHandle(-11);
            startup.Error = GetStdHandle(-12);
            ProcessInfo process;
            // Suspend before the child's first instruction, so it cannot create an
            // unowned descendant between Process.Start and job assignment.
            if (!CreateProcess(start.FileName,
                new StringBuilder(Quote(start.FileName) + " " + start.Arguments),
                IntPtr.Zero, IntPtr.Zero, true, 0x08000004, IntPtr.Zero, null,
                ref startup, out process))
                throw new IOException("Could not create the bundled tool process.");
            try
            {
                if (!AssignProcessToJobObject(job, process.Process))
                {
                    TerminateProcess(process.Process, 127);
                    throw new IOException("Could not attach the tool process to its job.");
                }
                if (ResumeThread(process.Thread) == uint.MaxValue)
                    throw new IOException("Could not resume the bundled tool process.");
                if (WaitForSingleObject(process.Process, uint.MaxValue) != 0)
                    throw new IOException("Could not wait for the bundled tool process.");
                uint exitCode;
                if (!GetExitCodeProcess(process.Process, out exitCode))
                    throw new IOException("Could not read the bundled tool exit code.");
                return unchecked((int)exitCode);
            }
            finally { CloseHandle(process.Thread); CloseHandle(process.Process); }
        }
        finally { CloseHandle(job); }
    }

    private static string Hash(string path)
    {
        using (var input = File.OpenRead(path))
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
    }

    private static string SafePath(string root, string relative)
    {
        if (Path.IsPathRooted(relative) || relative.IndexOf(':') >= 0)
            throw new IOException("Invalid bundled tool path.");
        if ((File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Bundled tool root is a redirected directory.");
        var path = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
        if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new IOException("Bundled tool path escaped its installation.");
        for (var cursor = path; cursor.Length > root.Length; cursor = Path.GetDirectoryName(cursor))
            if ((File.GetAttributes(cursor) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Bundled tool path contains a redirected file or directory.");
        return path;
    }

    // Literal Windows argv quoting, including trailing backslashes. No shell is used.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"')
                result.Append('\\', slashes * 2 + 1).Append('"');
            else
                result.Append('\\', slashes).Append(character);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    public static int Main(string[] args)
    {
        try
        {
            string bin = Path.GetDirectoryName(typeof(ToolLauncher).Assembly.Location);
            string root = Path.GetFullPath(Path.Combine(bin, ".."));
            string manifest = SafePath(root, "payload-manifest.json");
            if (Hash(manifest) != ManifestHash) throw new IOException("Bundled tool manifest changed.");
            var document = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(manifest));
            var files = (Dictionary<string, object>)document["files"];
            foreach (var entry in files)
                if (Hash(SafePath(root, entry.Key)) != (string)entry.Value)
                    throw new IOException("A bundled Power BI tool is damaged. Reinstall Modelcraft.");

            string executable;
            var arguments = new List<string>();
#if PBI
            executable = SafePath(root, "pbi/pbi.exe");
#elif AUTHOR
            executable = SafePath(root, "node/node.exe");
            arguments.Add(SafePath(root, "npm/node_modules/@microsoft/powerbi-report-authoring-cli/dist/cli.js"));
#else
            executable = SafePath(root, "node/node.exe");
            arguments.Add(SafePath(root, "npm/node_modules/@microsoft/powerbi-desktop-bridge-cli/dist/index.js"));
#endif
            arguments.AddRange(args);
            var quoted = new List<string>();
            foreach (string argument in arguments) quoted.Add(Quote(argument));
            var start = new ProcessStartInfo(executable, string.Join(" ", quoted.ToArray()));
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            return Run(start);
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Modelcraft Power BI tool could not start: " + error.Message);
            return 127;
        }
    }
}
