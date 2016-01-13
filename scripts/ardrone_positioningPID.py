#!/usr/bin/env python
import math
import rospy
import PID
import time
import sys
from geometry_msgs.msg import Twist
from tf2_msgs.msg import TFMessage
from std_msgs.msg import Empty            # for land/takeoff/emergency
from std_msgs.msg import Int8
from ardrone_autonomy.msg import Navdata  # for receiving navdata feedback
from keyboard_controller import KeyboardController
from visualization_msgs.msg import Marker # for Marker estimation from viewpoint_estimation
#from rescueranger.msg import CoordStruct

class DroneStatus(object):
    Emergency = 0
    Inited = 1
    Landed = 2
    Flying = 3
    Hovering = 4
    Test = 5
    TakingOff = 6
    GotoHover = 7
    Landing = 8
    Looping = 9


class ObjectTracker(object):

    def __init__(self):
        self.state = 0
        self.ardrone_state = -1
        self.ardrone_rotY = 0 #Forward/backward tilt in degrees (rotation about the Y axis)
        self.ardrone_velY = 0 #current velocity in left direction
        self.ardrone_velX = 0 #current velocity in left direction
        
        self.Px = 0.11
        self.Ix = 0.001
        self.Dx = 0.1
        self.saturation_x = 0.3
        
        self.Py = 0.13
        self.Iy = 0.001
        self.Dy = 0.1#0.035
        self.saturation_y = 0.35
        
        self.Pz = 1.1
        self.Iz = 0.01
        self.Dz = 0#0.05
        self.saturation_z = 0.9
        
        self.Pyaw = 1#0.75
        self.Iyaw = 0.2
        self.Dyaw = 0.2
        self.saturation_yaw = 0.6
        
        
        self.pidx = PID.PID(self.Px, self.Ix, self.Dx, self.saturation_x)
        self.pidy = PID.PID(self.Py, self.Iy, self.Dy, self.saturation_y)
        self.pidz = PID.PID(self.Pz, self.Iz, self.Dz, self.saturation_z)
        self.pidyaw = PID.PID(self.Pyaw, self.Iyaw, self.Dyaw, self.saturation_yaw)
        
        self.pidx.windup_guard = 0.1
        self.pidy.windup_guard = 0.1
        self.pidz.windup_guard = 0.1
        self.pidyaw.windup_guard = 2 #(tuned)
                
        self.smplTime = 1/300
        
        #these are in the markers frame, which has same orientation as the ardrone when it is straight in front of it
        #which means X points inwards in the marker, and the Y points left. Z points up. yaw is positive clockwise  
        self.goal_z_marker_prim_frame = 0.0
        self.goal_y_marker_prim_frame = 0.0
        self.goal_x_marker_prim_frame = -4.5
        self.goal_yaw_marker_frame = 0.0
        
        self.pidx.setSampleTime(self.smplTime)
        self.pidy.setSampleTime(self.smplTime)
        self.pidz.setSampleTime(self.smplTime)
        self.pidyaw.setSampleTime(self.smplTime)
        
        self.estimated_marker = None
        self.marker_seen = False
        self.marker_timeout_threshold = 0.2 # equivalent of not finding any marker in 4 consecutive images
        self.marker_last_seen = 0 - self.marker_timeout_threshold
        self.marker_last_seen_left = False
        
        print('Rescueranger initilizing')
        
        self.pub = rospy.Publisher(
            'object_tracker/pref_pos',
            Twist,
            queue_size=10)
        
        # Subscribe to the /ardrone/navdata topic, of message type navdata, and
        # call self.ReceiveNavdata when a message is received
        self.subNavdata = rospy.Subscriber(
            '/ardrone/navdata',
            Navdata,
            self.callback_ardrone_navdata)

        # Allow the controller to publish to the /ardrone/takeoff, land and
        # reset topics
        self.pubLand = rospy.Publisher('/ardrone/land', Empty, queue_size=10)
        self.pubTakeoff = rospy.Publisher('/ardrone/takeoff', Empty, queue_size=10)
        self.pubReset = rospy.Publisher('/ardrone/reset', Empty, queue_size=10)

        # Allow the controller to publish to the /cmd_vel topic and thus control
        # the drone
        self.command = Twist()
        self.pubCommand = rospy.Publisher('/cmd_vel', Twist, queue_size=10)
        
        ## Publish error relative the marker
        #self.pub_info_error = rospy.Publisher(
        #    '/rescueranger/MarkerError',
        #    CoordStruct,
        #    queue_size=10
        #    )
        #self.info_error = CoordStruct()
        
        ## Publish error relative the marker
        #self.pub_info_marker = rospy.Publisher(
        #    '/rescueranger/MarkerPos',
        #    CoordStruct,
        #    queue_size=10
        #    )
        #self.info_marker = CoordStruct()

        ## Publish error relative the marker
        #self.pub_info_vel = rospy.Publisher(
        #    '/rescueranger/VelocityOutput',
        #    CoordStruct,
        #    queue_size=10
        #    )
        #self.info_vel = CoordStruct()

        # Keyboard Controller
        self.keyboard = KeyboardController()
        self.keyboard.tracker = self

        print('...')
        rospy.init_node('object_tracker', anonymous=True)
            
        # Marker pose subscriber
        rospy.Subscriber(
            '/Estimated_marker',
            Marker,
            self.callback_estimated_marker_pose)

        # Marker State
        self.estimated_marker_state_map = {
            'Waiting': 0,  # Not detecting any pattern, just recieving images
            'Detected_marker': 1,  # Pattern detected
        }

        print('...initilized\n')
        rospy.on_shutdown(self.ardrone_send_land)

    def callback_ardrone_navdata(self, navdata):
        
        #get state
        if self.ardrone_state != navdata.state:
            print("Recieved droneState: %d" % navdata.state)
            self.ardrone_state = navdata.state

        #get tilt and velocity
        self.ardrone_rotY = navdata.rotY*math.pi/180 #from degrees to radians, positive anti-clockwise (tested)
        self.ardrone_velY = navdata.vy/1000 #from mm/s to m/s, positive left (tested)
        self.ardrone_velX = navdata.vx/1000 #from mm/s to m/s, positive forward (tested)

    def ardrone_send_takeoff(self):
        # Send a takeoff message to the ardrone driver
        # Note we only send a takeoff message if the drone is landed - an
        # unexpected takeoff is not good!
        if self.ardrone_state == DroneStatus.Landed:
            self.pubTakeoff.publish(Empty())

    def ardrone_send_land(self):
        # Send a landing message to the ardrone driver
        # Note we send this in all states, landing can do no harm
        self.pubLand.publish(Empty())

    def ardrone_send_emergency(self):
        # Send an emergency (or reset) message to the ardrone driver
        self.pubReset.publish(Empty())
        print("Publishing reset")

    def ardrone_set_xyzy(self, x=0, y=0, z=0, yaw=0):
        # Called by the main program to set the current command
        self.command.linear.x = x
        self.command.linear.y = y
        self.command.linear.z = z
        self.command.angular.z = yaw #turn anti-clockwise(tested)

    def ardrone_update_rpyz(self, event):
        # The previously set command is then sent out periodically if the drone
        # is flying
        if (
            self.ardrone_state == DroneStatus.Flying or
            self.ardrone_state == DroneStatus.GotoHover or
            self.ardrone_state == DroneStatus.Hovering
        ):
            self.pubCommand.publish(self.command)

    def ardrone_send_command(self,event):
        # The previously set command is then sent out periodically if the drone is flying
        if (
            self.ardrone_state == DroneStatus.Flying or 
            self.ardrone_state == DroneStatus.GotoHover or
            self.ardrone_state == DroneStatus.Hovering
        ):
            self.pubCommand.publish(self.command)

    # Predicted pose from tum_ardrone/drone_stateestimation, written to ardrone
    def callback_ardrone_prediction(self, pose):
        self.pred_pose = pose
    
    # Estimated marker position relative to the camera 
    def callback_estimated_marker_pose(self, Marker):
        self.marker_last_seen = time.time()
        self.estimated_marker = Marker
                
    #################################################################################
    #################################################################################
    # The main running loop
    # This is where all the intresting magic happens
    #################################################################################
    #################################################################################
    def run(self):
        twist = Twist()
        # Publish the estimated waypoint on object_tracker/pref_pos
        r = rospy.Rate(200)  # in Hz
        # 0=wait, 1=taking off, 2=search for marker,  3= aproach marker, 4= land
        while not rospy.is_shutdown():
            #sys.stderr.write("\x1b[2J\x1b[H")
            dt = time.time() - self.marker_last_seen
            if dt < self.marker_timeout_threshold:
                if self.marker_seen == False:
                    self.pidx.clear()
                    self.pidy.clear()
                    self.pidz.clear()
                    self.pidyaw.clear()
                    print("Marker found")
                self.marker_seen = True
            else:
                if self.marker_seen == True:
                    print("Marker lost") 
                self.marker_seen = False
            if self.state == 0: # wait for start command
                pass
            if self.state == 1:  # taking off
                self.ardrone_send_takeoff()
                if self.ardrone_state == DroneStatus.Hovering:
                    self.state = 2
                    print("Hovering")
                #self.state = 2 #TODO Remove this, this ignores actual flight!
            if self.state == 2:  # search for marker
                if self.marker_last_seen_left:
                    self.ardrone_set_xyzy(0, 0, 0, 0.5)
                else:
                    self.ardrone_set_xyzy(0, 0, 0, -0.5)
                if self.marker_seen:
                    self.state = 3
            if self.state == 3:  # aproach marker
                if self.marker_seen:
                    # Remap camera cooridnate frame to drone coordinate frame
                    # marker_x: Positive if marker is forward in drone frame
                    # marker_y: Positive if marker is left in drone frame
                    # marker_z: Positive if marker is up in drone frame
                    # Yaw: Positive if marker is anti- clockwise in drone frame
                    marker_z = -self.estimated_marker.pose.position.x
                    marker_y = -self.estimated_marker.pose.position.y
                    marker_x = self.estimated_marker.pose.position.z
                    q0 = self.estimated_marker.pose.orientation.w
                    q1 = self.estimated_marker.pose.orientation.x
                    q2 = self.estimated_marker.pose.orientation.y
                    q3 = self.estimated_marker.pose.orientation.z
                    marker_frame_yaw = -math.asin(2*(q0*q2 - q3*q1))
                    marker_frame_pitch = math.atan2(2*(q0*q1 + q2*q3),1-2*(q1**2 + q2**2))
                    marker_frame_pitch = marker_frame_pitch%(2*math.pi) -math.pi # positive when tilting the marker upwards
                    real_distance = (marker_x**2 + marker_y**2 + marker_z**2)**0.5
                    angle_to_marker_in_ardrone_frame = -math.asin(marker_y/real_distance)
                    self.marker_last_seen_left = angle_to_marker_in_ardrone_frame < 0
                    #print((round(marker_roll*180/math.pi),round(marker_pitch*180/math.pi),round(marker_frame_yaw*180/math.pi)))             
                    #print(marker_frame_pitch*180/math.pi) 
                    ## Calculate error       
                    #height_err = self.goal_z_marker_prim_frame - marker_z
                    #horizontal_err = self.goal_y_marker_prim_frame - marker_y
                    #distance_err = self.goal_x_marker_prim_frame - (marker_z**2 + marker_y**2 + marker_x**2)**0.5
                    #yaw_err = self.goal_yaw_marker_frame - marker_frame_yaw
                    ## Publish errors
                    #self.info_error.x = distance_err
                    #self.info_error.y = horizontal_err
                    #self.info_error.z = height_err
                    #self.info_error.yaw = yaw_err
                    #self.info_error.time = time.time()
                    #self.pub_info_error.publish(self.info_error)
                    
                    #introduce a new coordinate frame, marker_prim, attached to the marker but rotated with the drone. in this coordinate frame, we want to find the position of the drone relative the marker
                    drone_pos_relative_marker_prim_x = -marker_x
                    drone_pos_relative_marker_prim_y = -marker_y 
                    drone_pos_relative_marker_prim_z = -marker_z
                    ## Publish current
                    #self.info_marker.x = drone_pos_relative_marker_prim_x
                    #self.info_marker.y = drone_pos_relative_marker_prim_y
                    #self.info_marker.z = drone_pos_relative_marker_prim_z
                    #self.info_marker.yaw = angle_to_marker_in_ardrone_frame
                    #self.info_marker.time = time.time()
                    #self.pub_info_marker.publish(self.info_marker)

                    #calculate reference point in quadcopter coordinate using 2D reverse transform
                    pidx_reference = self.goal_x_marker_prim_frame*math.cos(marker_frame_yaw)#TODO uncomment + self.goal_y_marker_prim_frame*math.sin(marker_frame_yaw)
                    pidy_reference = -self.goal_x_marker_prim_frame*math.sin(marker_frame_yaw)#TODO uncomment + self.goal_y_marker_prim_frame*math.cos(marker_frame_yaw)

                    camera_fov_vertical = 0.55#0.70 # 40 degrees in radians
                    marker_size = 0.7
                    marker_fov_vertical = marker_size/real_distance #approximate 
                    max_allowed_vertical_marker_angle = (camera_fov_vertical - marker_fov_vertical)/2

                    if abs(marker_frame_pitch) < max_allowed_vertical_marker_angle:
                        reference_vertical_marker_angle = marker_frame_pitch # negative sign because if we tilt up, marker should be down and vice versa
                    else:
                        reference_vertical_marker_angle = math.copysign(max_allowed_vertical_marker_angle, marker_frame_pitch)
                    pidz_reference = self.goal_z_marker_prim_frame + real_distance*math.sin(reference_vertical_marker_angle)
                    pidyaw_reference = self.goal_yaw_marker_frame
                    
                    #update reference
                    self.pidx.setReference(pidx_reference)
                    self.pidy.setReference(pidy_reference)
                    self.pidz.setReference(pidz_reference)
                    self.pidyaw.setReference(pidyaw_reference)

                    # Update the PIDs
                    self.pidx.updatePID(drone_pos_relative_marker_prim_x)
                    self.pidy.updatePID(drone_pos_relative_marker_prim_y)
                    self.pidz.updatePID(drone_pos_relative_marker_prim_z)
                    self.pidyaw.updatePID(angle_to_marker_in_ardrone_frame)
                    
                    x_vel = self.pidx.output
                    y_vel = self.pidy.output
                    z_vel = self.pidz.output
                    yaw_vel = self.pidyaw.output -(self.ardrone_velY*math.cos( angle_to_marker_in_ardrone_frame) - self.ardrone_velX*math.sin( angle_to_marker_in_ardrone_frame) )/real_distance
                    print(z_vel)
                    
                    
                    ## Publish velocities
                    #self.info_vel.x = x_vel
                    #self.info_vel.y = y_vel
                    #self.info_vel.z = z_vel
                    #self.info_vel.yaw = yaw_vel
                    #self.info_vel.time = time.time()                   
                    #self.pub_info_vel.publish(self.info_vel)
                    
                    #print((angle_to_marker_in_ardrone_frame,yaw_vel))
                    #print((x_vel, y_vel, z_vel, yaw_vel, angle_to_marker_in_ardrone_frame))
                    #print((round(100*yaw_vel), round(100*self.pidyaw.ITerm)))
                    #print((self.pidx.last_error, self.pidy.last_error, self.pidz.last_error, self.pidyaw.last_error))
                    self.ardrone_set_xyzy(x_vel, y_vel, z_vel, yaw_vel)
                    #self.ardrone_set_xyzy(0, 0, 0, 0)
                else:
                    self.ardrone_set_xyzy(0, 0, 0, 0)
                    self.state = 2
            if self.state == 4:  # land
                if self.ardrone_state == DroneStatus.Landed:
                    self.state = 0
                    print("Landed")
                else:
                    self.ardrone_send_land()

            self.ardrone_update_rpyz(None)

            self.pub.publish(twist)
            r.sleep()
        print('\n\nRescuranger is terminating.\n')
        self.ardrone_send_emergency()
        # spin() simply keeps python from exiting until this node is stopped


if __name__ == '__main__':
    try:
        tracker = ObjectTracker()
        tracker.run()
    except rospy.ROSInterruptException:
        pass
