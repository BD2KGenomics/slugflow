workflow stderrWorkflow {
  String message
  call get_stderr as s1 { input: message=message }
  call copy_output { input: in_file=s1.check_this }
}

task get_stderr {
  String message

  command {
    >&2 echo "${message}"
  }

 output {
    File check_this = stderr()
 }
}

# comply with builtinTest
task copy_output {
  File in_file

  command {
    cp ${in_file} output.txt
  }

  output {
    File the_output = 'output.txt'
  }
}
