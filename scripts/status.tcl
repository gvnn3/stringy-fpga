# status.tcl - U250 (xcu250) board/device status over JTAG via Vivado Hardware
# Manager
# Read-only. Does NOT program the device.
# Run:  vivado -mode batch -source status.tcl
#  or:  vivado -mode tcl  then  source status.tcl
# Optional: pass a hw_server URL ->  vivado -mode batch -source status.tcl
# -tclargs <host:port>

set url "localhost:3121"
if {$argc >= 1} { set url [lindex $argv 0] }

proc hr {t} { puts "\n===== $t =====" }
proc kv {k v} { puts [format "  %-26s %s" $k $v] }

open_hw_manager
puts "Connecting to hw_server: $url"
connect_hw_server -url $url

set targets [get_hw_targets]
if {[llength $targets] == 0} {
    puts "ERROR: no JTAG hw_targets found (cable connected? hw_server running?)"
    disconnect_hw_server
    return
}

foreach tgt $targets {
    hr "TARGET  $tgt"
    current_hw_target $tgt
    if {[catch {open_hw_target} msg]} {
        puts "  could not open target: $msg  (in use by another session?)"
        continue
    }

    foreach dev [get_hw_devices] {
        current_hw_device $dev
        # refresh reads the device status registers over JTAG
        catch {refresh_hw_device -update_hw_probes false $dev}

        hr "DEVICE  $dev"
        kv "Part"        [get_property PART $dev]
        catch { kv "IDCODE"   [get_property REGISTER.IDCODE_HEX $dev] }
        catch { kv "DNA"      [get_property REGISTER.EFUSE.FUSE_DNA $dev] }

        # --- configuration / boot health (SLR0; U250 has 4 SLRs) ---
        hr "CONFIG HEALTH (SLR0)"
        foreach {label prop} {
            DONE_pin        {REGISTER.CONFIG_STATUS.SLR0.BIT[14]_DONE_PIN}
            End_of_startup  \
              {REGISTER.CONFIG_STATUS.SLR0.BIT[04]_END_OF_STARTUP_(EOS)_STATUS}
            PLL_lock        \
              {REGISTER.CONFIG_STATUS.SLR0.BIT[02]_PLL_LOCK_STATUS}
            CRC_error       {REGISTER.CONFIG_STATUS.SLR0.BIT[00]_CRC_ERROR}
            OverTemp_alarm  \
  {REGISTER.CONFIG_STATUS.SLR0.BIT[17]_SYSTEM_MONITOR_OVER-TEMP_ALARM_STATUS}
        } {
            if {![catch {get_property $prop $dev} val]} { kv $label $val }
        }
        catch { kv "BOOT_STATUS.SLR0"   [get_property \
          REGISTER.BOOT_STATUS.SLR0 $dev] }
        catch { kv "CONFIG_STATUS.SLR0" [get_property \
          REGISTER.CONFIG_STATUS.SLR0 $dev] }

        # --- SYSMON: die temperature + internal voltage rails (if exposed) ---
        set sm [get_hw_sysmons -quiet -of_objects $dev]
        if {[llength $sm]} {
            catch {refresh_hw_sysmon $sm}
            hr "SYSMON (die)"
            foreach {label prop unit} {
                Temperature TEMPERATURE "C"
                VCCINT      VCCINT      "V"
                VCCAUX      VCCAUX      "V"
                VCCBRAM     VCCBRAM     "V"
            } {
                if {![catch {get_property $prop $sm} val]} {
                    kv $label "$val $unit"
                }
            }
        } else {
            hr "SYSMON (die)"
            puts "  no hw_sysmon exposed by the current design"
        }

        # --- debug cores present in the running design ---
        hr "DEBUG CORES"
        kv "ILA cores" [llength [get_hw_ilas -quiet -of_objects $dev]]
        kv "VIO cores" [llength [get_hw_vios -quiet -of_objects $dev]]
    }
    catch {close_hw_target}
}

disconnect_hw_server
puts "\nDone (read-only; nothing was programmed)."
