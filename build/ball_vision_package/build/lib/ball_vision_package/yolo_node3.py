#!/usr/bin/env python3

from pathlib import Path
import sys
import cv2
import depthai as dai
import numpy as np
import time
import math
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy

class BallTracker(Node):
    def __init__(self):
        super().__init__('ball_tracker')
        
        # Create QoS profile for real-time data
        qos_profile = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Create ROS2 publisher with QoS profile
        self.cmd_vel_pub = self.create_publisher(Twist, 'cmd_vel', qos_profile)
        
        # Variables to store last known values
        self.last_depth = 0.0
        self.last_angle = 0.0
        self.ball_detected = False
        
        # Get model path
        self.nnPath = str((Path(__file__).parent / Path('/home/projects/depthai-python/examples/models/yolov8n_coco_640x352.blob')).resolve().absolute())
        if not Path(self.nnPath).exists():
            raise FileNotFoundError(f'Required file/s not found, please run "{sys.executable} install_requirements.py"')

        # Initialize DepthAI pipeline
        self.initialize_pipeline()
        
    def initialize_pipeline(self):
        # Create pipeline
        pipeline = dai.Pipeline()

        # Define sources and outputs
        camRgb = pipeline.create(dai.node.ColorCamera)
        spatialDetectionNetwork = pipeline.create(dai.node.YoloSpatialDetectionNetwork)
        monoLeft = pipeline.create(dai.node.MonoCamera)
        monoRight = pipeline.create(dai.node.MonoCamera)
        stereo = pipeline.create(dai.node.StereoDepth)

        xoutRgb = pipeline.create(dai.node.XLinkOut)
        nnOut = pipeline.create(dai.node.XLinkOut)
        xoutDepth = pipeline.create(dai.node.XLinkOut)

        xoutRgb.setStreamName("rgb")
        nnOut.setStreamName("nn")
        xoutDepth.setStreamName("depth")

        # Properties
        camRgb.setPreviewSize(640, 352)
        camRgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
        camRgb.setInterleaved(False)
        camRgb.setColorOrder(dai.ColorCameraProperties.ColorOrder.BGR)
        camRgb.setFps(40)

        # MonoCamera settings
        monoLeft.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        monoLeft.setBoardSocket(dai.CameraBoardSocket.LEFT)
        monoRight.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        monoRight.setBoardSocket(dai.CameraBoardSocket.RIGHT)

        # StereoDepth settings
        stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
        stereo.setDepthAlign(dai.CameraBoardSocket.RGB)
        stereo.setOutputSize(camRgb.getPreviewWidth(), camRgb.getPreviewHeight())

        # Network specific settings
        spatialDetectionNetwork.setBlobPath(self.nnPath)
        spatialDetectionNetwork.setConfidenceThreshold(0.001)
        spatialDetectionNetwork.input.setBlocking(False)
        spatialDetectionNetwork.setBoundingBoxScaleFactor(0.5)
        spatialDetectionNetwork.setDepthLowerThreshold(100)
        spatialDetectionNetwork.setDepthUpperThreshold(10000)
        spatialDetectionNetwork.setNumClasses(80)
        spatialDetectionNetwork.setCoordinateSize(4)
        spatialDetectionNetwork.setAnchors([])
        spatialDetectionNetwork.setAnchorMasks({})
        spatialDetectionNetwork.setIouThreshold(0.5)

        # Linking
        monoLeft.out.link(stereo.left)
        monoRight.out.link(stereo.right)

        camRgb.preview.link(spatialDetectionNetwork.input)
        spatialDetectionNetwork.passthrough.link(xoutRgb.input)
        spatialDetectionNetwork.out.link(nnOut.input)
        stereo.depth.link(xoutDepth.input)
        stereo.depth.link(spatialDetectionNetwork.inputDepth)

        self.pipeline = pipeline

    def calculate_angle(self, frame_width, x_pos):
        frame_center_x = frame_width // 2
        return ((x_pos - frame_center_x) / frame_center_x) * (69.4 / 2)

    def frameNorm(self, frame, bbox):
        normVals = np.full(len(bbox), frame.shape[0])
        normVals[::2] = frame.shape[1]
        return (np.clip(np.array(bbox), 0, 1) * normVals).astype(int)

    def publish_values(self):
        # Create Twist message
        cmd_vel_msg = Twist()
        cmd_vel_msg.linear.x = 0.0
        cmd_vel_msg.linear.y = 0.0
        cmd_vel_msg.linear.z = float(self.last_depth)
        cmd_vel_msg.angular.x = 0.0
        cmd_vel_msg.angular.y = 0.0
        cmd_vel_msg.angular.z = float(self.last_angle)
        
        # Publish twist message
        self.cmd_vel_pub.publish(cmd_vel_msg)
        
        # Print to terminal
        if self.ball_detected:
            self.get_logger().info(f'Ball found - Depth: {self.last_depth:.1f}mm, Angle: {self.last_angle:.1f}°')
        else:
            self.get_logger().warn(f'Ball not found - Last known - Depth: {self.last_depth:.1f}mm, Angle: {self.last_angle:.1f}°')

    def run(self):
        with dai.Device(self.pipeline) as device:
            # Output queues with smaller sizes for reduced latency
            qRgb = device.getOutputQueue(name="rgb", maxSize=2, blocking=False)
            qDet = device.getOutputQueue(name="nn", maxSize=2, blocking=False)
            qDepth = device.getOutputQueue(name="depth", maxSize=2, blocking=False)

            while rclpy.ok():
                inRgb = qRgb.get()
                inDet = qDet.get()

                if inRgb is not None:
                    frame = inRgb.getCvFrame()

                ball_found_this_frame = False
                if inDet is not None:
                    detections = inDet.detections

                    for detection in detections:
                        if detection.label == 32:  # Sports ball class
                            ball_found_this_frame = True
                            bbox = self.frameNorm(frame, (detection.xmin, detection.ymin, detection.xmax, detection.ymax))
                            
                            # Calculate center point
                            center_x = (bbox[0] + bbox[2]) // 2
                            
                            # Update values
                            self.last_angle = self.calculate_angle(frame.shape[1], center_x)
                            self.last_depth = detection.spatialCoordinates.z
                            self.ball_detected = True
                            
                            if frame is not None:
                                # Draw bounding box
                                cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                                
                                # Display information
                                cv2.putText(frame, f"D:{int(self.last_depth)}mm A:{self.last_angle:.1f}°",
                                           (bbox[0] + 10, bbox[1] + 20),
                                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

                if not ball_found_this_frame:
                    self.ball_detected = False

                # Publish values regardless of whether ball was detected
                self.publish_values()

                if frame is not None:
                    cv2.imshow("Ball Tracking", frame)

                if cv2.waitKey(1) == ord('q'):
                    break

            cv2.destroyAllWindows()

def main(args=None):
    rclpy.init(args=args)
    ball_tracker = BallTracker()
    
    try:
        ball_tracker.run()
    except KeyboardInterrupt:
        pass
    finally:
        ball_tracker.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
